"""Shared inbox admission and durable, authorization-checked response delivery."""

from datetime import UTC, datetime, timedelta
from uuid import UUID

from fastapi import HTTPException
from sqlalchemy import select, update

from backend.app.channels import authorized_access, claim_invitation
from backend.app.diagnostics import admit_employee_run, authorized_run
from backend.app.models import (
    ACTIVE_RUN_STATUSES,
    Agent,
    ChannelInbox,
    ChannelOutbox,
    EmployeeAccess,
    EmployeeChannel,
    Run,
)


class TransportError(Exception):
    def __init__(self, code: str, *, uncertain=False, retry_after: int | None = None):
        super().__init__(code)
        self.code, self.uncertain, self.retry_after = code, uncertain, retry_after


def split_response(value: str) -> list[str]:
    # 3000 UTF-16 code units also respects Telegram's 4096-character ceiling.
    # Bound very large runtime results without streaming unlimited messages.
    value = value[:24000]
    parts, current, size = [], [], 0
    for char in value:
        width = 2 if ord(char) > 0xFFFF else 1
        if size + width > 3000:
            parts.append("".join(current))
            current, size = [], 0
        current.append(char)
        size += width
    if current:
        parts.append("".join(current))
    return parts or ["El agente terminó sin una respuesta de texto."]


def ingest(
    session,
    channel_id: UUID,
    revision: int,
    event_id: str,
    user: str,
    scope: str,
    destination: str,
    message: str,
) -> ChannelInbox:
    """Called only by a provider adapter. Caller commits before acknowledging receipt."""
    previous = session.scalar(
        select(ChannelInbox).where(
            ChannelInbox.channel_id == channel_id, ChannelInbox.event_id == event_id
        )
    )
    if previous:
        return previous
    channel = session.get(EmployeeChannel, channel_id, populate_existing=True)
    inbox = ChannelInbox(
        channel_id=channel_id,
        channel_revision=revision,
        event_id=event_id,
        external_user_id=user,
        external_scope=scope,
        destination=destination,
        code="unauthorized",
    )
    session.add(inbox)
    session.flush()
    if not channel.enabled or channel.revision != revision or scope != channel.workspace_id:
        inbox.code = "channel_changed"
        return inbox
    response = None
    command, _, argument = message.strip().partition(" ")
    if command in ("/start", "/register") and argument:
        try:
            access = claim_invitation(session, channel_id, argument.strip(), user, scope)
            inbox.code = "identity_pending"
            response = "Identidad registrada. El administrador debe aprobar tu acceso."
        except HTTPException:
            inbox.code = "invitation_invalid"
            return inbox
    else:
        access = session.scalar(
            select(EmployeeAccess).where(
                EmployeeAccess.channel_id == channel_id,
                EmployeeAccess.external_scope == scope,
                EmployeeAccess.external_user_id == user,
            )
        )
        agent = session.get(Agent, access.agent_id) if access else None
        if agent is None or not authorized_access(
            session, access, agent, channel_revision=revision
        ):
            return inbox
        if message.lstrip().startswith("/"):
            inbox.code = "command"
            status_command = "agent status" if channel.provider == "slack" else "/status"
            help_command = "agent help" if channel.provider == "slack" else "/help"
            response = (
                f"Escribe un mensaje de texto para hablar con tu agente. {status_command} muestra su estado."
                if command in ("/help", "/start")
                else (
                    "Agente disponible."
                    if agent.observed_state == "ready"
                    else "Agente no disponible."
                )
                if command == "/status"
                else f"Comando no disponible. Usa {help_command} o escribe un mensaje de texto."
            )
        elif not message.strip() or len(message.strip()) > 4000 or "\x00" in message:
            inbox.code = "invalid_message"
            response = "Envía un mensaje de texto de hasta 4000 caracteres."
        else:
            try:
                run = admit_employee_run(
                    session, access.id, message, f"channel:{inbox.id}", channel_revision=revision
                )
                inbox.run_id = run.id
                inbox.code = "admitted"
            except HTTPException as error:
                inbox.code = "unauthorized" if error.status_code == 403 else "unavailable"
                if error.status_code == 403:
                    return inbox
                response = "Tu agente está ocupado o no disponible. Inténtalo más tarde."
    inbox.access_id, inbox.access_revision = access.id, access.revision
    session.add(
        ChannelOutbox(
            inbox_id=inbox.id,
            state="pending" if response else "waiting",
            parts=split_response(response) if response else [],
        )
    )
    return inbox


def inbox_authorized(session, inbox: ChannelInbox) -> bool:
    access = session.get(EmployeeAccess, inbox.access_id, populate_existing=True)
    channel = session.get(EmployeeChannel, inbox.channel_id, populate_existing=True)
    if (
        not access
        or access.revision != inbox.access_revision
        or access.external_user_id != inbox.external_user_id
        or access.external_scope != inbox.external_scope
        or not channel.enabled
        or channel.revision != inbox.channel_revision
    ):
        return False
    if inbox.code == "identity_pending":
        return access.state == "pending"
    agent = session.get(Agent, access.agent_id, populate_existing=True)
    if not agent or not authorized_access(
        session,
        access,
        agent,
        access_revision=inbox.access_revision,
        channel_revision=inbox.channel_revision,
    ):
        return False
    if inbox.run_id:
        run = session.get(Run, inbox.run_id)
        return agent.current_incarnation_id == run.incarnation_id and authorized_run(
            session, run, agent
        )
    return True


class Delivery:
    def __init__(self, sessions):
        self.sessions = sessions

    def recover(self):
        with self.sessions.begin() as session:
            session.execute(
                update(ChannelOutbox)
                .where(ChannelOutbox.state == "sending")
                .values(state="uncertain", code="connector_restarted")
            )

    def claim(self, channel_id: UUID):
        with self.sessions.begin() as session:
            outbox = session.scalar(
                select(ChannelOutbox)
                .join(ChannelInbox)
                .outerjoin(Run, Run.id == ChannelInbox.run_id)
                .where(
                    ChannelInbox.channel_id == channel_id,
                    (ChannelOutbox.state == "pending")
                    | (
                        (ChannelOutbox.state == "waiting")
                        & (Run.status.not_in(ACTIVE_RUN_STATUSES) | (Run.status == "unknown"))
                    ),
                    (ChannelOutbox.retry_at.is_(None))
                    | (ChannelOutbox.retry_at <= datetime.now(UTC)),
                )
                .order_by(ChannelInbox.created_at)
                .with_for_update(of=ChannelOutbox, skip_locked=True)
            )
            if outbox is None:
                return None
            inbox = session.get(ChannelInbox, outbox.inbox_id)
            if not inbox_authorized(session, inbox):
                outbox.state, outbox.code = "blocked", "access_changed"
                return None
            if outbox.state == "waiting":
                run = session.get(Run, inbox.run_id)
                outbox.parts = split_response(
                    run.output
                    if run.status == "completed"
                    else "No se pudo confirmar la respuesta del agente. "
                    "Consulta con tu administrador."
                )
            outbox.state = "sending"
            return outbox, inbox

    async def send_one(self, channel_id: UUID, transport):
        claimed = self.claim(channel_id)
        if claimed is None:
            return False
        outbox, inbox = claimed
        try:
            # Durable intent already committed. Send one part per tick to respect limits.
            with self.sessions() as session:
                if not inbox_authorized(session, session.get(ChannelInbox, inbox.id)):
                    self.finish(outbox.id, "blocked", "access_changed")
                    return True
            identifier = await transport.send(inbox.destination, outbox.parts[outbox.next_part])
        except TransportError as error:
            self.finish(
                outbox.id,
                "uncertain"
                if error.uncertain
                else "pending"
                if error.retry_after is not None
                else "failed",
                error.code,
                error.retry_after,
            )
        except Exception:
            # Adapter may have sent before failing. Never interpolate its exception.
            self.finish(outbox.id, "uncertain", "transport_uncertain")
        else:
            with self.sessions.begin() as session:
                current = session.get(ChannelOutbox, outbox.id, with_for_update=True)
                current.provider_ids = [*current.provider_ids, identifier]
                current.next_part += 1
                current.state = "sent" if current.next_part == len(current.parts) else "pending"
                current.retry_at = datetime.now(UTC) + timedelta(seconds=1)
                current.code = "provider_accepted" if current.state == "sent" else None
        return True

    def finish(self, identifier, state, code, retry_after=None):
        with self.sessions.begin() as session:
            row = session.get(ChannelOutbox, identifier, with_for_update=True)
            row.state, row.code = state, code
            row.retry_at = (
                datetime.now(UTC) + timedelta(seconds=max(1, min(retry_after, 3600)))
                if retry_after is not None
                else None
            )
