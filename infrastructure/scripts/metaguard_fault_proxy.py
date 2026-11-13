"""Disposable test-only network fault proxy; never installed as a production API."""
from collections import Counter
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import socket
import threading

import httpx

lock = threading.Lock()
controls = {}
posts = Counter()
acks = Counter()
held = set()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass  # Never log credentials or event bodies.

    def respond(self, status, body):
        self.send_response(status)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def dispatch(self):
        body = self.rfile.read(int(self.headers.get('Content-Length', '0')))
        if self.path == '/__faults':
            with lock:
                if self.command == 'POST':
                    controls.update(json.loads(body))
                data = {'controls': controls, 'cloud_posts': dict(posts), 'acknowledgements': dict(acks), 'held': sorted(held)}
                encoded = json.dumps(data).encode()
            return self.respond(200, encoded)
        if self.path == '/health':
            return self.respond(200, b'{"status":"healthy"}')
        prefix = '/controller' if self.path.startswith('/controller/') else '/patient'
        target = ('http://controller:8000' if prefix == '/controller' else 'http://patient:8000')
        path = self.path[len(prefix):]
        headers = {key: value for key, value in self.headers.items()
                   if key.lower() not in ('host', 'content-length', 'connection')}
        event_id = None
        is_ack = path.endswith('/acknowledge') and prefix == '/controller'
        try:
            if prefix == '/patient' and path == '/events' and self.command == 'POST':
                event_id = json.loads(body)['event_id']
                with lock:
                    posts[event_id] += 1
            if is_ack:
                gid = path.split('/')[2]
                lookup = httpx.get(target + '/grants/by-id/' + gid, headers=headers, timeout=10)
                if lookup.status_code == 200:
                    event_id = lookup.json()['event_id']
            response = httpx.request(self.command, target + path, headers=headers, content=body, timeout=15)
            drop = False
            with lock:
                if is_ack and response.status_code == 200 and event_id:
                    acks[event_id] += 1
                    if controls.get('drop_ack_for') == event_id:
                        controls.pop('drop_ack_for')
                        drop = True
                if prefix == '/patient' and path == '/events' and response.status_code in (200, 201):
                    if controls.get('drop_cloud_for') == event_id:
                        controls.pop('drop_cloud_for')
                        drop = True
                    hold = controls.get('hold_cloud_for') == event_id
                    if hold:
                        held.add(event_id)
                else:
                    hold = False
            if hold:
                # Upstream already committed; wait while the test kills the hospital.
                import time
                for _ in range(300):
                    with lock:
                        if controls.get('hold_cloud_for') != event_id:
                            break
                    time.sleep(.1)
            if drop:
                self.connection.shutdown(socket.SHUT_RDWR)
                self.connection.close()
                self.close_connection = True
                return
            self.respond(response.status_code, response.content)
        except (httpx.HTTPError, OSError):
            try:
                self.respond(503, b'{"detail":"Test upstream unavailable"}')
            except OSError:
                pass

    do_GET = dispatch
    do_POST = dispatch


if __name__ == '__main__':
    ThreadingHTTPServer(('0.0.0.0', 8000), Handler).serve_forever()
