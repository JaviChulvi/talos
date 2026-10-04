"""Single trusted connector process. A PostgreSQL lease prevents duplicate consumers."""

import asyncio
import logging
import signal
from contextlib import suppress
from datetime import UTC, datetime

from sqlalchemy import select, text

from backend.app.availability import heartbeat
from backend.app.connections import Connection, ConnectionBindingError, load_bound_secrets
from backend.app.db import get_engine, session_factory
from backend.app.installation import writable
from backend.app.models import ChannelCursor, ChannelProbe, UserChannel
from connector.delivery import Delivery, TransportError
from connector.slack import Slack
from connector.telegram import Telegram


class Connector:
    def __init__(self, sessions):
        self.sessions = sessions
        self.delivery = Delivery(sessions)
        self.tasks = {}
        self.transports = {}
        self.check_task = None
        self.checking = set()

    def configurations(self):
        with self.sessions() as session:
            if not writable(session):
                return []
            return [
                (channel, session.get(Connection, channel.connection_id))
                for channel in session.scalars(
                    select(UserChannel).where(UserChannel.enabled.is_(True))
                )
            ]

    def report(
        self, channel_id, revision, version, state, code, identity=None, *, allow_disabled=False
    ):
        with self.sessions.begin() as session:
            if not writable(session):
                return False
            channel = session.get(UserChannel, channel_id)
            # Same connection -> channel order as credential rotation.
            connection = session.get(
                Connection, channel.connection_id, with_for_update=True, populate_existing=True
            )
            channel = session.get(
                UserChannel, channel_id, with_for_update=True, populate_existing=True
            )
            if (
                (not channel.enabled and not allow_disabled)
                or channel.revision != revision
                or connection.current_version_id != version
            ):
                return False
            cursor = session.get(ChannelCursor, channel_id)
            if cursor is None:
                cursor = ChannelCursor(channel_id=channel_id)
                session.add(cursor)
            cursor.credential_version_id, cursor.revision = version, revision
            cursor.state, cursor.code, cursor.checked_at = state, code, datetime.now(UTC)
            if identity:
                if channel.enabled and channel.provider == "slack" and cursor.reconnect_required:
                    cursor.accept_after = datetime.now(UTC)
                    cursor.reconnect_required = False
                if cursor.provider_identity != identity["bot_id"]:
                    cursor.offset = 0
                cursor.provider_identity = identity["bot_id"]
                channel.identity, channel.verified_version_id = identity, version
                channel.verified_at = datetime.now(UTC)
            elif state == "blocked":
                channel.verified_version_id = None
            return True

    async def consume(self, channel, version):
        transport = None
        delay = 1
        try:
            secrets = load_bound_secrets(
                {
                    "channel": {
                        "version_id": str(version),
                        "fields": ["bot_token"]
                        if channel.provider == "telegram"
                        else ["bot_token", "app_token"],
                    }
                }
            )["channel"]
            if channel.provider == "slack":
                await self.consume_slack(channel, version, secrets)
                return
            transport = Telegram(secrets["bot_token"])
            while True:
                try:
                    identity = await transport.verify()
                    if not self.report(
                        channel.id, channel.revision, version, "checking", "connecting", identity
                    ):
                        return
                    self.transports[channel.id] = transport
                    while True:
                        with self.sessions() as session:
                            cursor = session.get(ChannelCursor, channel.id)
                            offset = -1 if cursor.reconnect_required else cursor.offset
                            reconnect = cursor.reconnect_required
                        updates = await transport.poll(offset)
                        if reconnect:
                            with self.sessions.begin() as session:
                                if not writable(session):
                                    return
                                cursor = session.get(
                                    ChannelCursor, channel.id, with_for_update=True
                                )
                                if any(
                                    not isinstance(update, dict)
                                    or type(update.get("update_id")) is not int
                                    for update in updates
                                ):
                                    raise TransportError("invalid_updates")
                                cursor.offset = max(
                                    (update["update_id"] + 1 for update in updates), default=0
                                )
                                cursor.accept_after = datetime.now(UTC)
                                cursor.reconnect_required = False
                            continue

                        for update in updates:
                            if (
                                not isinstance(update, dict)
                                or type(update.get("update_id")) is not int
                            ):
                                raise TransportError("invalid_updates")
                            with self.sessions.begin() as session:
                                if not writable(session):
                                    return
                                current = session.get(
                                    UserChannel, channel.id, populate_existing=True
                                )
                                if not current.enabled or current.revision != channel.revision:
                                    return
                                cursor = session.get(ChannelCursor, channel.id)
                                if update["update_id"] >= cursor.offset:
                                    transport.receive(
                                        session,
                                        current,
                                        channel.revision,
                                        identity["bot_id"],
                                        update,
                                    )
                                    cursor.offset = update["update_id"] + 1
                        if not self.report(
                            channel.id, channel.revision, version, "ok", "polling_active"
                        ):
                            return
                        delay = 1
                except TransportError as error:
                    self.transports.pop(channel.id, None)
                    state = (
                        "blocked"
                        if error.code
                        in (
                            "invalid_credentials",
                            "permission_denied",
                            "consumer_conflict",
                            "webhook_conflict",
                        )
                        else "unknown"
                    )
                    if not self.report(channel.id, channel.revision, version, state, error.code):
                        return
                    await asyncio.sleep(error.retry_after or delay)
                    delay = min(delay * 2, 30)
        except ConnectionBindingError:
            self.report(channel.id, channel.revision, version, "blocked", "credentials_unavailable")
        finally:
            self.transports.pop(channel.id, None)
            if transport:
                await transport.close()

    async def consume_slack(self, channel, version, secrets):
        delay = 1
        while True:
            transport = Slack(secrets["bot_token"], secrets["app_token"])
            try:
                identity = await transport.verify(channel.workspace_id)
                if not self.report(channel.id, channel.revision, version, "checking", "connecting"):
                    return
                await transport.connect(
                    self.sessions,
                    channel,
                    verified=lambda identity=identity: self.report(
                        channel.id, channel.revision, version, "ok", "socket_active", identity
                    ),
                )
                self.transports[channel.id] = transport
                while True:
                    if not await transport.connected():
                        raise TransportError("socket_disconnected")
                    if not self.report(
                        channel.id, channel.revision, version, "ok", "socket_active"
                    ):
                        return
                    delay = 1
                    await asyncio.sleep(5)
            except TransportError as error:
                state = (
                    "blocked"
                    if error.code
                    in (
                        "invalid_credentials",
                        "invalid_token_type",
                        "missing_scope",
                        "permission_denied",
                        "app_token_mismatch",
                        "workspace_mismatch",
                        "app_uninstalled",
                        "consumer_conflict",
                    )
                    else "unknown"
                )
                if not self.report(channel.id, channel.revision, version, state, error.code):
                    return
            finally:
                self.transports.pop(channel.id, None)
                await transport.close()
            await asyncio.sleep(delay)
            delay = min(delay * 2, 30)

    async def tick(self):
        if self.check_task is not None and self.check_task.done():
            self.check_task.result()
            self.check_task = None
        if self.check_task is None:
            self.check_task = asyncio.create_task(self.process_check())
            # Let the check claim its channel before reconciling consumers.
            await asyncio.sleep(0)
        configurations = {
            channel.id: (channel, connection.current_version_id)
            for channel, connection in self.configurations()
            if connection.current_version_id and channel.id not in self.checking
        }
        for identifier, (revision, version, task) in list(self.tasks.items()):
            desired = configurations.get(identifier)
            if (
                task.done()
                or not desired
                or (desired[0].revision, desired[1]) != (revision, version)
            ):
                task.cancel()
                with suppress(asyncio.CancelledError):
                    await task
                del self.tasks[identifier]
        for identifier, (channel, version) in configurations.items():
            if identifier not in self.tasks:
                self.tasks[identifier] = (
                    channel.revision,
                    version,
                    asyncio.create_task(self.consume(channel, version)),
                )
        with self.sessions.begin() as session:
            heartbeat(session, "connector")
        await asyncio.gather(
            *(
                self.delivery.send_one(identifier, transport)
                for identifier, transport in list(self.transports.items())
                if identifier not in self.checking
            )
        )

    def recover(self):
        self.delivery.recover()
        with self.sessions.begin() as session:
            for probe in session.scalars(
                select(ChannelProbe).where(ChannelProbe.status == "running")
            ):
                probe.status, probe.code = "failed", "connector_restarted"
                probe.completed_at = datetime.now(UTC)

    async def process_check(self):
        with self.sessions.begin() as session:
            if not writable(session):
                return
            probe = session.scalar(
                select(ChannelProbe)
                .where(ChannelProbe.status == "queued")
                .order_by(ChannelProbe.created_at)
                .with_for_update(skip_locked=True)
            )
            if probe is None:
                return
            channel = session.get(UserChannel, probe.channel_id)
            probe.status = "running"
        # A single owner replaces its existing consumer while checking a channel.
        self.checking.add(channel.id)
        existing = self.tasks.pop(channel.id, None)
        transport = None
        code, status = "channel_changed", "stale"
        try:
            if existing:
                existing[2].cancel()
                await asyncio.gather(existing[2], return_exceptions=True)
            if not self.report(
                channel.id,
                probe.revision,
                probe.credential_version_id,
                "checking",
                "probe_running",
                allow_disabled=True,
            ):
                return
            fields = ["bot_token"] if channel.provider == "telegram" else ["bot_token", "app_token"]
            credentials = load_bound_secrets(
                {"channel": {"version_id": str(probe.credential_version_id), "fields": fields}}
            )["channel"]
            transport = (
                Telegram(credentials["bot_token"])
                if channel.provider == "telegram"
                else Slack(credentials["bot_token"], credentials["app_token"])
            )
            identity = await transport.verify(channel.workspace_id)

            def verified():
                return self.report(
                    channel.id,
                    probe.revision,
                    probe.credential_version_id,
                    "ok",
                    "socket_active" if channel.enabled else "credentials_verified",
                    identity,
                    allow_disabled=True,
                )

            if channel.provider == "slack":
                await transport.connect(self.sessions, channel, verified=verified)
                if not await transport.connected():
                    raise TransportError("socket_disconnected")
            elif verified():
                if channel.enabled:
                    with self.sessions() as session:
                        cursor = session.get(ChannelCursor, channel.id)
                        offset = -1 if cursor.reconnect_required else cursor.offset
                        reconnect = cursor.reconnect_required
                    updates = await transport.poll(offset)
                    if reconnect:
                        updates = []
                    for update in updates:
                        if not isinstance(update, dict) or type(update.get("update_id")) is not int:
                            raise TransportError("invalid_updates")
                        with self.sessions.begin() as session:
                            cursor = session.get(ChannelCursor, channel.id)
                            if update["update_id"] >= cursor.offset:
                                transport.receive(
                                    session, channel, probe.revision, identity["bot_id"], update
                                )
                                cursor.offset = update["update_id"] + 1
                    self.report(
                        channel.id,
                        probe.revision,
                        probe.credential_version_id,
                        "ok",
                        "polling_active",
                    )
            code, status = (
                "credentials_and_transport_verified" if channel.enabled else "credentials_verified",
                "completed",
            )
        except asyncio.CancelledError:
            code, status = "check_interrupted", "failed"
            raise
        except TransportError as error:
            code, status = error.code, "failed"
            self.report(
                channel.id,
                probe.revision,
                probe.credential_version_id,
                "blocked",
                code,
                allow_disabled=True,
            )
        except ConnectionBindingError:
            code, status = "credentials_unavailable", "failed"
            self.report(
                channel.id,
                probe.revision,
                probe.credential_version_id,
                "blocked",
                code,
                allow_disabled=True,
            )
        finally:
            if transport:
                await transport.close()
            from backend.app.channels import channel_row

            with self.sessions.begin() as session:
                current = channel_row(session, channel.id, lock=True)
                connection = session.get(Connection, current.connection_id)
                row = session.get(ChannelProbe, probe.id, with_for_update=True)
                changed = (
                    current.revision != probe.revision
                    or connection.current_version_id != probe.credential_version_id
                )
                row.status, row.code = ("stale", "channel_changed") if changed else (status, code)
                row.completed_at = datetime.now(UTC)
            self.checking.discard(channel.id)

    async def close(self):
        tasks = [task for _, _, task in self.tasks.values()]
        if self.check_task is not None:
            tasks.append(self.check_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)


async def run():
    stop = asyncio.Event()
    for signum in (signal.SIGINT, signal.SIGTERM):
        asyncio.get_running_loop().add_signal_handler(signum, stop.set)
    # Hold the same physical connection for the process lifetime. Losing it ends
    # this process, so a replacement can recover durable send intent safely.
    with get_engine().connect() as lease:
        if not lease.scalar(text("SELECT pg_try_advisory_lock(1413565519, 1)")):
            raise RuntimeError("Another messaging connector owns this installation")
        pid = lease.scalar(text("SELECT pg_backend_pid()"))
        lease.commit()
        connector = Connector(session_factory())
        try:
            connector.recover()
            while not stop.is_set():
                if lease.scalar(text("SELECT pg_backend_pid()")) != pid:
                    raise RuntimeError("Messaging connector database lease lost")
                lease.commit()
                await connector.tick()
                try:
                    await asyncio.wait_for(stop.wait(), timeout=1)
                except TimeoutError:
                    pass
        finally:
            await connector.close()
            with suppress(Exception):
                lease.execute(text("SELECT pg_advisory_unlock(1413565519, 1)"))
                lease.commit()


def main():
    logging.basicConfig(level=logging.WARNING)
    # httpx INFO logs include Telegram token-bearing request paths.
    logging.getLogger("httpx").setLevel(logging.CRITICAL)
    logging.getLogger("httpcore").setLevel(logging.CRITICAL)
    try:
        asyncio.run(run())
    except Exception:
        logging.error(
            "Messaging connector stopped; inspect channel checks and database availability"
        )
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
