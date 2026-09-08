"""
The dashboard vhost (infra/ansible/roles/nginx/templates/dashboard.conf.j2)
publishes the Pi's admin panel through nginx. What it must hold, rendered:

- it proxies to the PANEL port (8088 by default), not live.py's 8080;
- Basic Auth is on for the vhost and off ONLY under the share prefix;
- every proxied location SETS X-Forwarded-For / -Proto / -Host from
  nginx's own knowledge - `$remote_addr`, never the appending
  `$proxy_add_x_forwarded_for` - because the panel passes this hop's
  values through as the truth and live.py builds share links from them;
- Authorization is not stripped toward the upstream, so the credential
  in htpasswd and the one in the panel's config.json are one login;
- the SSE streams keep their buffering-off treatment.

Rendered with jinja2 when it is installed (the same engine Ansible uses);
otherwise with a minimal `{{ var }}` substitution, which is enough for
this template - it has no control structures - and is reported as such.
No Ansible, no network, no VPS.
"""
import os
import re
import unittest

from tests import support

ROLE = os.path.join(support.ROOT, "infra", "ansible", "roles", "nginx")
TEMPLATE = os.path.join(ROLE, "templates", "dashboard.conf.j2")
OFFLINE = os.path.join(ROLE, "templates", "offline.html.j2")
TASKS = os.path.join(ROLE, "tasks", "main.yml")

#: The facts the role sets from infra/.env, at their defaults.
FACTS = {
    "dashboard_domain": "f10.example.com",
    "pi_wg_ip": "10.77.0.10",
    "pi_dashboard_port": "8088",
}

THREE = ("X-Forwarded-For", "X-Forwarded-Proto", "X-Forwarded-Host")


def render(source: str, facts: dict) -> str:
    """jinja2 if present; a `{{ name }}` substitution otherwise."""
    try:
        import jinja2  # type: ignore
    except ImportError:
        jinja2 = None

    if jinja2 is not None:
        env = jinja2.Environment(undefined=jinja2.StrictUndefined)
        return env.from_string(source).render(**facts)

    if re.search(r"{%", source):
        raise unittest.SkipTest(
            "jinja2 is not installed and the template has control "
            "structures - install jinja2 to render it")

    def sub(match):
        name = match.group(1)
        if name not in facts:
            raise AssertionError(f"undefined variable {name!r}")
        return facts[name]

    return re.sub(r"{{\s*(\w+)\s*}}", sub, source)


def locations(conf: str):
    """
    {location path: block body} for every `location` in the rendered
    file, matched on braces. Nothing here is nested, and no path or
    value in the template contains a brace.
    """
    found = {}
    for match in re.finditer(r"^\s*location\s+(=\s+)?(\S+)\s*{", conf, re.M):
        depth, i = 1, match.end()
        while depth:
            if conf[i] == "{":
                depth += 1
            elif conf[i] == "}":
                depth -= 1
            i += 1
        found[match.group(2)] = conf[match.end():i - 1]
    return found


def directives(block: str, name: str):
    """Every `name ...;` line in a block, as its argument string."""
    return [
        line.strip()[len(name):].strip().rstrip(";").strip()
        for line in block.splitlines()
        if line.strip().startswith(name + " ") or line.strip() == name + ";"
    ]


class TheRenderedVhost(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(TEMPLATE, encoding="utf-8") as fh:
            cls.source = fh.read()
        cls.rendered = render(cls.source, FACTS)
        #: The directives, without the comments that explain them.
        cls.conf = "\n".join(line for line in cls.rendered.splitlines()
                             if not line.strip().startswith("#"))
        cls.locations = locations(cls.conf)
        cls.proxied = {
            path: block for path, block in cls.locations.items()
            if directives(block, "proxy_pass")
        }

    def test_renders_with_nothing_undefined(self):
        self.assertNotIn("{{", self.rendered)
        self.assertNotIn("{%", self.rendered)
        self.assertIn("server_name f10.example.com;", self.conf)

    def test_every_proxied_location_targets_the_panel(self):
        """Four proxied locations, all to the Pi's wg0 address and the
        panel port - live.py's 8080 is on the Pi's loopback."""
        self.assertEqual(set(self.proxied),
                         {"/", "/s/", "/s/api/stream", "/api/stream"})

        for path, block in self.proxied.items():
            self.assertEqual(directives(block, "proxy_pass"),
                             ["http://10.77.0.10:8088"], path)

        self.assertNotIn(":8080", self.conf)

    def test_every_proxied_location_sets_the_three_forwarded_headers(self):
        """
        SET from nginx's own knowledge, once each, replacing the
        client's: the panel passes this hop's values through unchanged,
        so an appended client value would let a phone choose the
        public name a share link is minted with.
        """
        want = {
            "X-Forwarded-For": "$remote_addr",
            "X-Forwarded-Proto": "$scheme",
            "X-Forwarded-Host": "$host",
        }

        for path, block in self.proxied.items():
            headers = {}
            for arg in directives(block, "proxy_set_header"):
                name, _, value = arg.partition(" ")
                self.assertNotIn(name, headers, f"{path}: {name} set twice")
                headers[name] = value.strip()

            for name in THREE:
                self.assertEqual(headers.get(name), want[name],
                                 f"{path} must set {name} {want[name]}")

        self.assertNotIn("proxy_add_x_forwarded_for", self.conf)

    def test_authorization_reaches_the_upstream(self):
        """
        One login: nginx forwards the Authorization header by default,
        and nothing here clears it - not per location, not for the
        server. The panel's own Basic Auth then sees the same credential
        that satisfied the htpasswd.
        """
        for arg in directives(self.conf, "proxy_set_header"):
            self.assertFalse(arg.lower().startswith("authorization"), arg)

        self.assertNotIn("proxy_hide_header", self.conf)
        self.assertNotIn("proxy_pass_header", self.conf)
        self.assertNotRegex(self.conf, r"(?i)proxy_set_header\s+Authorization")

    def test_basic_auth_is_on_and_off_only_under_the_share_prefix(self):
        server_level = self.conf.split("location", 1)[0]

        self.assertIn('auth_basic           "F10 dashboard";', server_level)
        self.assertIn("auth_basic_user_file /etc/nginx/.htpasswd-dashboard;",
                      server_level)

        off = {path for path, block in self.locations.items()
               if "off" in directives(block, "auth_basic")}

        self.assertEqual(off, {"/s/", "/s/api/stream"})

        for path in ("/", "/api/stream", "/__offline.html"):
            self.assertNotIn("auth_basic", self.locations[path], path)

    def test_the_streams_keep_the_sse_treatment(self):
        for path in ("/api/stream", "/s/api/stream"):
            block = self.proxied[path]

            self.assertEqual(directives(block, "proxy_buffering"), ["off"])
            self.assertEqual(directives(block, "proxy_cache"), ["off"])
            self.assertEqual(directives(block, "proxy_read_timeout"), ["1h"])
            self.assertEqual(directives(block, "chunked_transfer_encoding"),
                             ["off"])
            self.assertEqual(directives(block, "proxy_set_header Connection"),
                             ['""'])

        for path in ("/", "/s/"):
            self.assertEqual(directives(self.proxied[path],
                                        "proxy_read_timeout"), ["120s"])

    def test_the_share_prefix_is_kept_out_of_search_indexes(self):
        for path in ("/s/", "/s/api/stream"):
            self.assertIn('add_header X-Robots-Tag "noindex, nofollow" always;',
                          self.proxied[path])

    def test_tls_and_the_offline_page(self):
        self.assertIn("listen 443 ssl http2;", self.conf)
        self.assertIn("/etc/letsencrypt/live/f10.example.com/fullchain.pem",
                      self.conf)
        self.assertIn("Strict-Transport-Security", self.conf)
        self.assertIn("proxy_connect_timeout 5s;", self.conf)
        self.assertIn("error_page 502 503 504 /__offline.html;", self.conf)
        self.assertIn("internal;", self.locations["/__offline.html"])

    def test_the_port_is_a_variable_with_the_panel_as_its_default(self):
        """The role's fact, not the template, carries the default."""
        with open(TASKS, encoding="utf-8") as fh:
            tasks = fh.read()

        self.assertIn("pi_dashboard_port: \"{{ dotenv.get('PI_DASHBOARD_PORT', '')"
                      " | default('8088', true) }}\"", tasks)
        self.assertNotIn("default('8080'", tasks)
        self.assertIn("{{ pi_dashboard_port }}", self.source)

    def test_the_role_still_refuses_to_publish_without_a_password(self):
        with open(TASKS, encoding="utf-8") as fh:
            tasks = fh.read()

        self.assertIn("dashboard_auth_password | length > 0", tasks)
        self.assertIn("dashboard_auth_password != 'change-me-dashboard-password'",
                      tasks)
        self.assertIn("when: dashboard_domain | length > 0", tasks)


class TheOfflinePage(unittest.TestCase):
    """
    Served to share viewers too, so it is public in effect: it says the
    Pi is unreachable and names no address, key or vehicle identifier.
    """

    @classmethod
    def setUpClass(cls):
        with open(OFFLINE, encoding="utf-8") as fh:
            cls.source = fh.read()
        #: The Jinja comment is the only template syntax in it.
        cls.page = re.sub(r"{#.*?#}", "", cls.source, flags=re.S)

    def test_it_says_the_car_is_unreachable(self):
        self.assertIn("unreachable", self.page.lower())

    def test_it_renders_to_plain_html(self):
        self.assertNotIn("{{", self.page)
        self.assertNotIn("{%", self.page)
        self.assertTrue(self.page.lstrip().startswith("<!doctype html>"))

    def test_it_names_nothing(self):
        self.assertNotRegex(self.page, r"\b\d{1,3}(\.\d{1,3}){3}\b")
        self.assertNotRegex(self.page, r"(?i)\bwg0\b|wireguard|10\.77|8088|8080")
        self.assertNotRegex(self.page, r"(?i)\bvin\b|bmw|f10|520d|n47")
        self.assertNotRegex(self.page, r"(?i)https?://")

    def test_it_is_self_contained(self):
        self.assertNotRegex(self.page, r'(?i)<(link|script)[^>]*\b(href|src)=')


if __name__ == "__main__":
    unittest.main()
