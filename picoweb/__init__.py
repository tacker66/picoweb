
# Picoweb web pico-framework for Pycopy, https://github.com/pfalcon/pycopy
# Copyright (c) 2014-2020 Paul Sokolovsky
# SPDX-License-Identifier: MIT

# stripped-down micropython version (c) 2023 Thomas Ackermann

import gc
import re
import errno
import asyncio
import pkg_resources

def unquote_plus(s):
    s = s.replace("+", " ")
    arr = s.split("%")
    arr2 = [chr(int(x[:2], 16)) + x[2:] for x in arr[1:]]
    return arr[0] + "".join(arr2)

def parse_qs(s):
    res = {}
    if s:
        pairs = s.split("&")
        for p in pairs:
            vals = [unquote_plus(x) for x in p.split("=", 1)]
            if len(vals) == 1:
                vals.append(True)
            old = res.get(vals[0])
            if old is not None:
                if not isinstance(old, list):
                    old = [old]
                    res[vals[0]] = old
                old.append(vals[1])
            else:
                res[vals[0]] = vals[1]
    return res

SEND_BUFSZ = 128

def get_mime_type(fname):
    if fname.endswith(".html"):
        return "text/html"
    if fname.endswith(".css"):
        return "text/css"
    if fname.endswith(".png") or fname.endswith(".jpg"):
        return "image"
    return "text/plain"

def sendstream(writer, f):
    buf = bytearray(SEND_BUFSZ)
    while True:
        l = f.readinto(buf)
        if not l:
            break
        yield from writer.awrite(buf, 0, l)

def jsonify(writer, dict):
    import ujson
    yield from start_response(writer, "application/json")
    yield from writer.awrite(ujson.dumps(dict))

def start_response(writer, content_type="text/html; charset=utf-8", status="200", headers=None):
    yield from writer.awrite("HTTP/1.0 %s NA\r\n" % status)
    yield from writer.awrite("Content-Type: ")
    yield from writer.awrite(content_type)
    if not headers:
        yield from writer.awrite("\r\n\r\n")
        return
    yield from writer.awrite("\r\n")
    if isinstance(headers, bytes) or isinstance(headers, str):
        yield from writer.awrite(headers)
    else:
        for k, v in headers.items():
            yield from writer.awrite(k)
            yield from writer.awrite(": ")
            yield from writer.awrite(v)
            yield from writer.awrite("\r\n")
    yield from writer.awrite("\r\n")

def http_error(writer, status):
    yield from start_response(writer, status=status)
    yield from writer.awrite(status)

class HTTPRequest:

    def __init__(self):
        pass

    def read_form_data(self):
        size = int(self.headers[b"Content-Length"])
        data = yield from self.reader.readexactly(size)
        form = parse_qs(data.decode())
        self.form = form

    def parse_qs(self):
        form = parse_qs(self.qs)
        self.form = form

class WebApp:

    def __init__(self, pkg, routes=None, serve_static=True):
        if routes:
            self.url_map = routes
        else:
            self.url_map = []
        if pkg and pkg != "__main__":
            self.pkg = pkg.split(".", 1)[0]
        else:
            self.pkg = None
        if serve_static:
            self.url_map.append((re.compile("^/(static/.+)"), self.handle_static))
        self.mounts = []
        self.inited = False
        self.template_loader = None
        self.headers_mode = "parse"

    def parse_headers(self, reader):
        headers = {}
        while True:
            l = yield from reader.readline()
            if l == b"\r\n":
                break
            k, v = l.split(b":", 1)
            headers[k] = v.strip()
        return headers

    def _handle(self, reader, writer):
        close = True
        req = None
        try:
            request_line = yield from reader.readline()
            if request_line == b"":
                yield from writer.aclose()
                return
            req = HTTPRequest()
            request_line = request_line.decode()
            method, path, proto = request_line.split()
            path = path.split("?", 1)
            qs = ""
            if len(path) > 1:
                qs = path[1]
            path = path[0]

            app = self
            while True:
                found = False
                for subapp in app.mounts:
                    root = subapp.url
                    if path[:len(root)] == root:
                        app = subapp
                        found = True
                        path = path[len(root):]
                        if not path.startswith("/"):
                            path = "/" + path
                        break
                if not found:
                    break

            if not app.inited:
                app.init()

            found = False
            for e in app.url_map:
                pattern = e[0]
                handler = e[1]
                extra = {}
                if len(e) > 2:
                    extra = e[2]
                if path == pattern:
                    found = True
                    break
                elif not isinstance(pattern, str):
                    m = pattern.match(path)
                    if m:
                        req.url_match = m
                        found = True
                        break

            if not found:
                headers_mode = "skip"
            else:
                headers_mode = extra.get("headers", self.headers_mode)

            if headers_mode == "skip":
                while True:
                    l = yield from reader.readline()
                    if l == b"\r\n":
                        break
            elif headers_mode == "parse":
                req.headers = yield from self.parse_headers(reader)
            else:
                assert headers_mode == "leave"

            if found:
                req.method = method
                req.path = path
                req.qs = qs
                req.reader = reader
                close = yield from handler(req, writer)
            else:
                yield from start_response(writer, status="404")
                yield from writer.awrite("404\r\n")
        except Exception as e:
            yield from self.handle_exc(req, writer, e)

        if close is not False:
            yield from writer.aclose()

    def handle_exc(self, req, resp, e):
        return
        yield

    def mount(self, url, app):
        app.url = url
        self.mounts.append(app)
        self.mounts.sort(key=lambda app: len(app.url), reverse=True)

    def route(self, url, **kwargs):
        def _route(f):
            self.url_map.append((url, f, kwargs))
            return f
        return _route

    def add_url_rule(self, url, func, **kwargs):
        self.url_map.append((url, func, kwargs))

    def _load_template(self, tmpl_name):
        if self.template_loader is None:
            import utemplate.source
            self.template_loader = utemplate.source.Loader(self.pkg, "templates")
        return self.template_loader.load(tmpl_name)

    def render_template(self, writer, tmpl_name, args=()):
        tmpl = self._load_template(tmpl_name)
        for s in tmpl(*args):
            yield from writer.awritestr(s)

    def render_str(self, tmpl_name, args=()):
        tmpl = self._load_template(tmpl_name)
        return ''.join(tmpl(*args))

    def sendfile(self, writer, fname, content_type=None, headers=None):
        if not content_type:
            content_type = get_mime_type(fname)
        try:
            with pkg_resources.resource_stream(self.pkg, fname) as f:
                yield from start_response(writer, content_type, "200", headers)
                yield from sendstream(writer, f)
        except OSError as e:
            if e.args[0] == uerrno.ENOENT:
                yield from http_error(writer, "404")
            else:
                raise

    def handle_static(self, req, resp):
        path = req.url_match.group(1)
        print(path)
        if ".." in path:
            yield from http_error(resp, "403")
            return
        yield from self.sendfile(resp, path)

    def init(self):
        self.inited = True

    def serve(self, loop, host, port):
        loop.create_task(asyncio.start_server(self._handle, host, port))
        loop.run_forever()

    def run(self, host="0.0.0.0", port=80, lazy_init=False):
        gc.collect()
        self.init()
        if not lazy_init:
            for app in self.mounts:
                app.init()
        loop = asyncio.get_event_loop()
        self.serve(loop, host, port)
        loop.close()
