// Native UIs share a loopback hostname with Talos, but must never receive its cookie.
const http = require('node:http');
const target = { hostname: process.argv[1], port: Number(process.argv[2]) };
const adminCookie = process.argv[3];

function nativeHeaders(headers) {
  const result = { ...headers };
  if (result.cookie) {
    result.cookie = result.cookie.split(';')
      .filter(part => part.split('=', 1)[0].trim() !== adminCookie).join(';');
    if (!result.cookie.trim()) delete result.cookie;
  }
  return result;
}

function browserHeaders(headers) {
  const result = { ...headers };
  if (result['set-cookie']) {
    result['set-cookie'] = result['set-cookie']
      .filter(value => value.split('=', 1)[0].trim() !== adminCookie);
    if (!result['set-cookie'].length) delete result['set-cookie'];
  }
  return result;
}

function forward(response, downstream) {
  downstream.writeHead(response.statusCode, response.statusMessage, browserHeaders(response.headers));
  response.on('error', () => downstream.destroy());
  response.pipe(downstream);
}

const server = http.createServer((request, response) => {
  const upstream = http.request({ ...target, method: request.method, path: request.url,
    headers: nativeHeaders(request.headers) }, incoming => forward(incoming, response));
  upstream.on('error', () => {
    if (!response.headersSent) response.writeHead(502);
    response.end();
  });
  request.on('aborted', () => upstream.destroy());
  response.on('close', () => upstream.destroy());
  request.pipe(upstream);
});

server.on('upgrade', (request, socket, head) => {
  const upstream = http.request({ ...target, method: request.method, path: request.url,
    headers: nativeHeaders(request.headers) });
  socket.on('error', () => upstream.destroy());
  socket.on('close', () => upstream.destroy());
  upstream.on('error', () => socket.destroy());
  upstream.on('response', incoming => {
    const response = new http.ServerResponse(request);
    response.assignSocket(socket);
    forward(incoming, response);
  });
  upstream.on('upgrade', (incoming, peer, upstreamHead) => {
    const headers = Object.entries(browserHeaders(incoming.headers))
      .flatMap(([name, values]) => (Array.isArray(values) ? values : [values])
        .map(value => `${name}: ${value}\r\n`)).join('');
    socket.write(`HTTP/1.1 ${incoming.statusCode} ${incoming.statusMessage}\r\n${headers}\r\n`);
    if (head.length) peer.write(head);
    if (upstreamHead.length) socket.write(upstreamHead);
    socket.on('error', () => peer.destroy());
    peer.on('error', () => socket.destroy());
    socket.on('close', () => peer.destroy());
    peer.on('close', () => socket.destroy());
    socket.pipe(peer);
    peer.pipe(socket);
  });
  upstream.end();
});

server.listen(Number(process.argv[4] || 18789), '0.0.0.0', () => {
  console.log(server.address().port);
});
