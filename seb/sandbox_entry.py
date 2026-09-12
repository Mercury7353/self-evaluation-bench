"""Runs INSIDE bwrap. Loopback only, bridged to the metered gateway Unix socket."""
import fcntl
import json
import os
import selectors
import socket
import socketserver
import struct
import subprocess
import sys
import threading

class Proxy(socketserver.BaseRequestHandler):
    def handle(self):
        upstream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        upstream.connect(self.server.upstream_socket)
        sockets = [self.request, upstream]
        with selectors.DefaultSelector() as sel:
            for s in sockets: sel.register(s, selectors.EVENT_READ)
            try:
                while True:
                    for key, _ in sel.select():
                        data = key.fileobj.recv(65536)
                        if not data: return
                        sockets[1 if key.fileobj is sockets[0] else 0].sendall(data)
            finally: upstream.close()

class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address=True
    daemon_threads=True

args=json.load(open('/run/launch.json'))
if args.get('enable_loopback',True):
    s=socket.socket(socket.AF_INET,socket.SOCK_DGRAM)
    fcntl.ioctl(s.fileno(),0x8914,struct.pack('16sh',b'lo',0x49));s.close()
server=Server(('127.0.0.1',args.get('proxy_port',18765)),Proxy)
server.upstream_socket='/run/gateway.sock'
threading.Thread(target=server.serve_forever,daemon=True).start()
for bridge in args.get('extra_proxies',[]):
    extra=Server(('127.0.0.1',bridge['port']),Proxy)
    extra.upstream_socket=bridge['socket']
    threading.Thread(target=extra.serve_forever,daemon=True).start()
os.environ.update(args.get('env',{}))
result=subprocess.run(args['command'],cwd=args.get('cwd','/workspace'))
sys.exit(result.returncode)
