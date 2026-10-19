"""Ticketed loopback playback of validated project proxy files, with byte ranges."""
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import mimetypes
from pathlib import Path
import secrets
import threading
import re

class PreviewServer:
    def __init__(self):
        self.files={}
        owner=self
        class Handler(BaseHTTPRequestHandler):
            def log_message(self,*args): pass
            def do_GET(self): self.send_file(False)
            def do_HEAD(self): self.send_file(True)
            def send_file(self,head):
                path=owner.files.get(self.path)
                if path is None or not path.is_file(): self.send_error(404); return
                size=path.stat().st_size; start=0; end=size-1
                value=self.headers.get('Range')
                if value:
                    match=re.fullmatch(r'bytes=(\d+)-(\d*)',value)
                    if not match: self.send_error(416); return
                    start=int(match[1]); end=min(size-1,int(match[2]) if match[2] else size-1)
                    if start>end or start>=size: self.send_error(416); return
                self.send_response(206 if value else 200)
                self.send_header('Content-Type',mimetypes.guess_type(path.name)[0] or 'application/octet-stream'); self.send_header('Accept-Ranges','bytes')
                self.send_header('Content-Length',str(end-start+1)); self.send_header('Cache-Control','no-store')
                if value: self.send_header('Content-Range',f'bytes {start}-{end}/{size}')
                self.end_headers()
                if head: return
                try:
                    with path.open('rb') as stream:
                        stream.seek(start); left=end-start+1
                        while left:
                            data=stream.read(min(left,256*1024))
                            if not data: break
                            self.wfile.write(data); left-=len(data)
                except (BrokenPipeError,ConnectionResetError): pass
        self.server=ThreadingHTTPServer(('127.0.0.1',0),Handler)
        self.server.daemon_threads=True
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
    def register(self,path:Path):
        # Called only with a file from an already checksum-validated proxy bundle.
        for ticket,existing in self.files.items():
            if existing==path: return f'http://127.0.0.1:{self.server.server_port}{ticket}'
        ticket='/'+secrets.token_urlsafe(32); self.files[ticket]=path
        return f'http://127.0.0.1:{self.server.server_port}{ticket}'
    def close(self): self.server.shutdown(); self.server.server_close(); self.thread.join()
