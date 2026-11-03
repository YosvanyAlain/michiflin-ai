import json, os, socket, socketserver, sys
secret, path=sys.argv[1:]
class H(socketserver.StreamRequestHandler):
 def handle(self):
  while self.rfile.readline().strip():pass
  inet_denied=False
  try:
   with socket.socket(socket.AF_INET,socket.SOCK_STREAM) as sock:sock.settimeout(1);sock.connect(('1.1.1.1',443))
  except OSError:inet_denied=True
  result={'identity_separated':os.getuid()!=0,'workspace_hidden':not os.path.exists(secret),
          'outbound_blocked':inet_denied,'model_readable':open('/opt/michiflin-isolation-probe/model.txt').read()=='public model',
          'credentials_absent':not any(k in os.environ for k in ('GITHUB_TOKEN','HF_TOKEN','STATE_BACKUP_PASSPHRASE'))}
  body=json.dumps(result).encode();self.wfile.write(b'HTTP/1.1 200 OK\r\nContent-Type: application/json\r\nContent-Length: '+str(len(body)).encode()+b'\r\nConnection: close\r\n\r\n'+body)
socketserver.UnixStreamServer(path,H).serve_forever()
