import http.server, json, os, socket, sys
secret=sys.argv[1]
class H(http.server.BaseHTTPRequestHandler):
 def log_message(self,*_):pass
 def do_GET(self):
  reachable=False
  try:
   with socket.create_connection(('1.1.1.1',443),timeout=1):reachable=True
  except OSError:pass
  result={'identity_separated':os.getuid()!=0,'workspace_hidden':not os.path.exists(secret),
          'outbound_blocked':not reachable,'model_readable':open('/opt/michiflin-isolation-probe/model.txt').read()=='public model',
          'credentials_absent':not any(k in os.environ for k in ('GITHUB_TOKEN','HF_TOKEN','STATE_BACKUP_PASSPHRASE'))}
  body=json.dumps(result).encode();self.send_response(200);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body)
http.server.HTTPServer(('127.0.0.1',18971),H).serve_forever()
