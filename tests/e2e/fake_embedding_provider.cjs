/* Disposable OpenAI-compatible embeddings fixture for browser acceptance. */
const fs = require('node:fs');
const https = require('node:https');

const [certPath, keyPath, portFile] = process.argv.slice(2);
if (!certPath || !keyPath || !portFile) throw new Error('usage: fake_embedding_provider.cjs CERT KEY PORT_FILE');
const server = https.createServer({ cert: fs.readFileSync(certPath), key: fs.readFileSync(keyPath) }, (request, response) => {
  if (request.method === 'GET' && request.url === '/models') {
    response.writeHead(200, { 'content-type': 'application/json' });
    response.end(JSON.stringify({ data: [{ id: 'issue17-embedding' }] }));
    return;
  }
  if (request.method === 'POST' && request.url === '/embeddings') {
    let body = '';
    request.setEncoding('utf8');
    request.on('data', (chunk) => { body += chunk; });
    request.on('end', () => {
      let inputCount = 1;
      try {
        const parsed = JSON.parse(body);
        if (Array.isArray(parsed.input)) inputCount = parsed.input.length || 1;
      } catch (_) {
        inputCount = 1;
      }
      const data = Array.from({ length: inputCount }, (_, index) => ({
        index,
        embedding: [1, ...Array(1023).fill(0)],
      }));
      response.writeHead(200, { 'content-type': 'application/json' });
      response.end(JSON.stringify({ data }));
    });
    return;
  }
  response.writeHead(404, { 'content-type': 'application/json' });
  response.end(JSON.stringify({ error: 'not found' }));
});
server.listen(0, '127.0.0.1', () => {
  const address = server.address();
  if (!address || typeof address === 'string') throw new Error('fixture did not bind a TCP port');
  fs.writeFileSync(portFile, String(address.port));
});
const shutdown = () => server.close(() => process.exit(0));
process.on('SIGTERM', shutdown);
process.on('SIGINT', shutdown);
