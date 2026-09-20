#!/usr/bin/env python3
"""
Migrate tasks from a Vikunja JSON export into DoneTick chores via the DoneTick API.

What is migrated
  - title, description (HTML converted to Markdown-ish text), comments (appended to description)
  - due date and recurrence (Vikunja repeat_after / repeat_mode -> DoneTick frequency)
  - priority (mapped, see PRIORITY_MAP)
  - labels incl. colors
  - projects -> DoneTick projects (--project-mode projects, default). If DoneTick projects
    are flat, the hierarchy is kept in the name: "Home / Kitchen". If your DoneTick version
    supports nested projects, pass --project-parent-field <field> (see --inspect output) to
    create real nesting. Alternative: --project-mode labels|both|none.

Not migrated: buckets, views, attachments, reminders, related tasks, assignees.

Usage (stdlib only, Python 3.9+):
  1) python3 vikunja_to_donetick.py --url http://NAS:2021 --username U --password P --inspect
  2) python3 vikunja_to_donetick.py --export export.json --user-id 1              # dry run
  3) python3 vikunja_to_donetick.py --export export.json --user-id 1 --url ... --username ... --password ... --limit 1 --apply
  4) same without --limit
"""
import argparse
import html as _html
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser

# Vikunja: 0 none, 1 low, 2 medium, 3 high, 4 urgent, 5 DO NOW
# DoneTick: 0 none, 1 = highest ... 4 = lowest  (ADJUST if your instance differs)
PRIORITY_MAP = {0: 0, 1: 4, 2: 3, 3: 2, 4: 1, 5: 1}


# --------------------------------------------------------------------------- HTML -> text
class _H2T(HTMLParser):
    BLOCK = ("p", "div", "br", "h1", "h2", "h3", "h4", "blockquote", "pre")

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.buf = ""
        self.lists = []
        self.href = None
        self.after_bullet = False

    def _nl(self):
        if self.after_bullet or not self.buf or self.buf.endswith("\n"):
            return
        self.buf += "\n"

    def _w(self, s):
        self.buf += s
        self.after_bullet = False

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.BLOCK:
            self._nl()
        if tag in ("h1", "h2", "h3", "h4", "strong", "b"):
            self._w("**")
        elif tag in ("ul", "ol"):
            self.lists.append([tag, 0])
            self._nl()
        elif tag == "li":
            self._nl()
            depth = max(len(self.lists) - 1, 0)
            if a.get("data-type") == "taskItem":
                bullet = "- [x] " if a.get("data-checked") == "true" else "- [ ] "
            elif self.lists and self.lists[-1][0] == "ol":
                self.lists[-1][1] += 1
                bullet = f"{self.lists[-1][1]}. "
            else:
                bullet = "- "
            self.buf += "  " * depth + bullet
            self.after_bullet = True
        elif tag in ("em", "i"):
            self._w("_")
        elif tag == "code":
            self._w("`")
        elif tag == "a":
            self.href = a.get("href")
            self._w("[")

    def handle_endtag(self, tag):
        if tag in ("h1", "h2", "h3", "h4"):
            self._w("**")
            self._nl()
        elif tag in ("strong", "b"):
            self._w("**")
        elif tag in ("em", "i"):
            self._w("_")
        elif tag == "code":
            self._w("`")
        elif tag == "a":
            self._w(f"]({self.href})" if self.href else "]")
            self.href = None
        elif tag in ("ul", "ol"):
            if self.lists:
                self.lists.pop()
            self._nl()
        elif tag in ("p", "div", "li", "blockquote", "pre"):
            self._nl()

    def handle_data(self, d):
        d = re.sub(r"\s+", " ", d)
        if not d.strip() and (self.after_bullet or not self.buf or self.buf.endswith("\n")):
            return
        self._w(d)


def html_to_text(s):
    if not s:
        return ""
    p = _H2T()
    p.feed(s)
    return re.sub(r"\n{3,}", "\n\n", p.buf).strip()


# --------------------------------------------------------------------------- helpers
class _Clean(HTMLParser):
    """Reduce Vikunja's editor HTML to plain, attribute-free HTML that any rich-text editor understands."""

    ALLOWED = {"p", "br", "strong", "b", "em", "i", "u", "s", "code", "pre", "blockquote",
               "ul", "ol", "li", "a", "h1", "h2", "h3", "h4", "hr"}
    VOID = {"br", "hr", "img", "input", "meta", "link"}
    SKIP = {"script", "style"}

    def __init__(self, inline=False):
        super().__init__(convert_charrefs=True)
        self.out = []
        self.stack = []  # (tag, closer, is_li, is_skip)
        self.inline = inline

    def _in_li(self):
        return self.inline or any(x[2] for x in self.stack)

    def _skipping(self):
        return any(x[3] for x in self.stack)

    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        if tag in self.VOID:
            if tag in ("br", "hr") and not self._skipping():
                self.out.append(f"<{tag}>")
            return
        if tag in self.SKIP:
            self.stack.append((tag, "", False, True))
        elif tag == "li":
            self.out.append("<li>")
            if a.get("data-type") == "taskItem":
                self.out.append("\u2611 " if a.get("data-checked") == "true" else "\u2610 ")
            self.stack.append((tag, "</li>", True, False))
        elif tag == "p" and self._in_li():
            self.stack.append((tag, " ", False, False))
        elif tag in self.ALLOWED:
            if tag == "a":
                href = a.get("href")
                self.out.append(f'<a href="{_html.escape(href, quote=True)}">' if href else "<a>")
            else:
                self.out.append(f"<{tag}>")
            self.stack.append((tag, f"</{tag}>", False, False))
        else:
            self.stack.append((tag, "", False, False))  # div, span, label, ...: unwrap

    def handle_startendtag(self, tag, attrs):
        if tag in ("br", "hr") and not self._skipping():
            self.out.append(f"<{tag}>")

    def handle_endtag(self, tag):
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                for entry in reversed(self.stack[i:]):
                    self.out.append(entry[1])
                del self.stack[i:]
                return

    def handle_data(self, d):
        if not self._skipping():
            self.out.append(_html.escape(d))


def clean_html(s, inline=False):
    if not s:
        return ""
    c = _Clean(inline)
    c.feed(s)
    out = "".join(c.out)
    out = re.sub(r"<p>\s*</p>", "", out)
    out = re.sub(r"(</li>)\s+", r"\1", out)
    out = re.sub(r"\s+(</li>)", r"\1", out)
    return out.strip()


def plain_to_html(s):
    return "".join(f"<p>{_html.escape(l)}</p>" for l in (s or "").split("\n") if l.strip())


def md_to_plain(t):
    t = re.sub(r"\*\*(.+?)\*\*", r"\1", t, flags=re.S)
    t = re.sub(r"(?<!\w)_(.+?)_(?!\w)", r"\1", t)
    t = t.replace("`", "")
    return re.sub(r"\[([^\]]*)\]\(([^)]+)\)", r"\1 (\2)", t)


def parse_dt(s):
    if not s or str(s).startswith("0001"):
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def iso_utc(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def color(hexstr):
    hexstr = (hexstr or "").lstrip("#")
    return f"#{hexstr}" if hexstr else ""


def unwrap(x):
    return x.get("res", x) if isinstance(x, dict) else x


def load_projects(path):
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    projects = data if isinstance(data, list) else data.get("projects", [])
    by_id = {}

    def walk(p):
        by_id[p["id"]] = p
        for c in p.get("child_projects") or []:
            walk(c)

    for p in projects:
        walk(p)
    return by_id


def project_chain(pid, by_id, warned):
    chain, seen = [], set()
    while pid and pid > 0 and pid in by_id and pid not in seen:
        seen.add(pid)
        chain.append(by_id[pid])
        pid = by_id[pid].get("parent_project_id")
    if pid and pid > 0 and pid not in by_id and pid not in warned:
        warned.add(pid)
        print(f"  ! parent project {pid} not found in export; treating its child as top-level")
    return list(reversed(chain))


def map_recurrence(task, due, tz):
    """Return (frequencyType, frequency, metadata, isRolling)."""
    after = int(task.get("repeat_after") or 0)
    mode = int(task.get("repeat_mode") or 0)
    meta = {"timezone": tz}
    if due:
        meta["time"] = iso_utc(due)
    if mode == 1:  # monthly
        return "monthly", 1, meta, False
    if after <= 0:
        return "once", 1, meta, False
    rolling = mode == 2  # repeat from completion date
    if after % 86400 == 0:
        days = after // 86400
        if days % 7 == 0:
            meta["unit"] = "weeks"
            return "interval", days // 7, meta, rolling
        meta["unit"] = "days"
        return "interval", days, meta, rolling
    meta["unit"] = "hours"
    return "interval", max(after // 3600, 1), meta, rolling


def build_description(task, fmt="html"):
    raw = task.get("description") or ""
    comments = task.get("comments") or []
    tid = task["id"]

    def who(c):
        au = c.get("author") or {}
        return au.get("name") or au.get("username") or "?"

    if fmt == "html":
        body = clean_html(raw) if re.search(r"<[a-zA-Z][^>]*>", raw) else plain_to_html(raw)
        parts = [body]
        if comments:
            items = "".join(
                f"<li><strong>{_html.escape(who(c))}</strong> ({(c.get('created') or '')[:10]}): "
                f"{clean_html(c.get('comment'), inline=True)}</li>"
                for c in comments
            )
            parts.append(f"<p><strong>Comments from Vikunja</strong></p><ul>{items}</ul>")
        parts.append(f"<p><em>Migrated from Vikunja task #{tid}</em></p>")
        return "".join(p for p in parts if p)

    parts = [html_to_text(raw)]
    if comments:
        lines = ["**Comments from Vikunja**"]
        for c in comments:
            lines.append(f"- {who(c)} ({(c.get('created') or '')[:10]}): {html_to_text(c.get('comment'))}")
        parts.append("\n".join(lines))
    parts.append(f"_Migrated from Vikunja task #{tid}_")
    text = "\n\n".join(p for p in parts if p)
    return md_to_plain(text) if fmt == "text" else text


# --------------------------------------------------------------------------- API client
class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class DoneTick:
    def __init__(self, base, token=None, api_key=None):
        self.base = base.rstrip("/")
        self.token = token
        self.api_key = api_key

    def req(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        url = self.base + path
        opener = urllib.request.build_opener(_NoRedirect)
        for _ in range(4):  # follow redirects manually so POST bodies are preserved
            r = urllib.request.Request(url, data=data, method=method)
            r.add_header("Content-Type", "application/json")
            if self.token:
                r.add_header("Authorization", "Bearer " + self.token)
            if self.api_key:
                r.add_header("secretkey", self.api_key)
            try:
                with opener.open(r, timeout=30) as resp:
                    raw = resp.read()
                return json.loads(raw) if raw else None
            except urllib.error.HTTPError as e:
                loc = e.headers.get("Location")
                if e.code in (301, 302, 303, 307, 308) and loc:
                    nxt = urllib.parse.urljoin(url, loc)
                    # keep the scheme we were configured with (reverse proxies sometimes downgrade it)
                    if urllib.parse.urlparse(nxt).scheme != urllib.parse.urlparse(self.base).scheme:
                        nxt = urllib.parse.urlparse(self.base).scheme + nxt[nxt.index("://"):]
                    url = nxt
                    continue
                raise RuntimeError(f"{method} {path} -> HTTP {e.code}: {e.read().decode(errors='replace')[:500]}")
        raise RuntimeError(f"{method} {path} -> too many redirects")

    def login(self, username, password):
        last = None
        for path in ("/api/v1/auth/login", "/auth/login"):
            try:
                out = self.req("POST", path, {"username": username, "password": password})
                self.token = out["token"]
                return
            except Exception as e:  # noqa: BLE001
                last = e
        raise RuntimeError(f"Login failed: {last}")

    def labels(self):
        return unwrap(self.req("GET", "/api/v1/labels")) or []

    projects_path = None

    def projects(self):
        last = None
        for path in ([self.projects_path] if self.projects_path else ["/api/v1/projects/", "/api/v1/projects"]):
            try:
                out = self.req("GET", path)
                self.projects_path = path
                return unwrap(out) or []
            except RuntimeError as e:
                last = e
        raise last

    def chores(self):
        return unwrap(self.req("GET", "/api/v1/chores/")) or []


# --------------------------------------------------------------------------- main
def wipe(dt, args, by_id):
    """Delete chores, projects and labels from DoneTick (dry run unless --apply)."""
    scope = args.wipe
    marker = "Migrated from Vikunja task #"

    # --- chores
    sel_c = []
    for c in dt.chores():
        if scope == "all":
            sel_c.append(c)
            continue
        desc = c.get("description")
        if desc is None:  # list response may omit it
            try:
                detail = unwrap(dt.req("GET", f"/api/v1/chores/{c['id']}"))
                desc = detail.get("description") if isinstance(detail, dict) else ""
            except RuntimeError:
                desc = ""
        if marker in (desc or ""):
            sel_c.append(c)

    # --- projects and labels
    projects = dt.projects()
    labels = dt.labels()
    if scope == "all":
        sel_p, sel_l = projects, labels
    else:
        names_p, names_l = set(), set()
        for pid in by_id:
            acc = []
            for proj in project_chain(pid, by_id, set()):
                acc.append(proj["title"])
                names_p.add(" / ".join(acc))
                names_p.add(proj["title"])
                names_l.add(args.label_prefix + " / ".join(acc))
        for pr in by_id.values():
            for t in pr.get("tasks") or []:
                for lb in t.get("labels") or []:
                    names_l.add(lb["title"])
        sel_p = [x for x in projects if x.get("name") in names_p]
        sel_l = [x for x in labels if x.get("name") in names_l]

    def show(title, items, key):
        print(f"\n{title}: {len(items)}")
        for it in items[:15]:
            print(f"  - {it.get(key)}")
        if len(items) > 15:
            print(f"  ... and {len(items) - 15} more")

    print(f"Wipe scope: {scope}")
    show("Chores to delete", sel_c, "name")
    show("Projects to delete", sel_p, "name")
    show("Labels to delete", sel_l, "name")

    if not (sel_c or sel_p or sel_l):
        print("\nNothing to delete.")
        return
    if not args.apply:
        print("\nDry run only. Add --apply to delete.")
        return
    if not args.yes:
        if input("\nType DELETE to permanently remove the items above: ").strip() != "DELETE":
            sys.exit("Aborted.")

    failed = 0
    for kind, path, items in (("chore", "chores", sel_c), ("project", "projects", sel_p), ("label", "labels", sel_l)):
        for it in items:
            try:
                dt.req("DELETE", f"/api/v1/{path}/{it['id']}")
            except RuntimeError as e:
                failed += 1
                print(f"  FAILED {kind} '{it.get('name')}': {e}")
            time.sleep(0.1)
    if os.path.exists(args.state):
        os.remove(args.state)
        print(f"Removed state file {args.state}.")
    print(f"Wipe finished ({failed} failure(s)). You can now run the import again.")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--export", help="Vikunja JSON export file")
    ap.add_argument("--url", help="DoneTick base URL, e.g. http://192.168.1.10:2021")
    ap.add_argument("--username", default=os.getenv("DT_USERNAME"))
    ap.add_argument("--password", default=os.getenv("DT_PASSWORD"))
    ap.add_argument("--token", default=os.getenv("DT_TOKEN"), help="JWT token (alternative to username/password)")
    ap.add_argument("--api-key", default=os.getenv("DT_API_KEY"), help="DoneTick API key (sent as 'secretkey' header)")
    ap.add_argument("--user-id", type=int, help="DoneTick user id chores are assigned to")
    ap.add_argument("--timezone", default="Europe/Berlin")
    ap.add_argument("--project-mode", choices=["projects", "labels", "both", "none"], default="projects",
                    help="how Vikunja projects are mapped (default: DoneTick projects)")
    ap.add_argument("--project-parent-field", default=None,
                    help="JSON field DoneTick uses for a project's parent (e.g. parentId); enables real nesting")
    ap.add_argument("--label-prefix", default="", help="prefix for project labels (labels/both mode)")
    ap.add_argument("--include-done", action="store_true", help="also import done tasks (as inactive chores)")
    ap.add_argument("--include-archived", action="store_true", help="also import archived projects")
    ap.add_argument("--private", action="store_true",
                    help="set isPrivate=true on every chore (UI: Privacy Settings 'Limited', if that is the mapped field)")
    ap.add_argument("--set", action="append", default=[], metavar="KEY=JSON",
                    help="set any extra field on every chore, e.g. --set isPrivate=true --set points=5 (repeatable)")
    ap.add_argument("--wipe", choices=["migrated", "all"],
                    help="delete chores/projects/labels from DoneTick and exit. 'migrated' = only chores created by this "
                         "script (marker in description) plus projects/labels named in --export; 'all' = EVERYTHING")
    ap.add_argument("--yes", action="store_true", help="skip the typed DELETE confirmation for --wipe --apply")
    ap.add_argument("--description-format", choices=["html", "markdown", "text"], default="html",
                    help="how descriptions are written (default html; use markdown/text if the UI shows html tags)")
    ap.add_argument("--no-labels", action="store_true", help="do not create/attach labels (loses project hierarchy)")
    ap.add_argument("--limit", type=int, help="only import the first N tasks (for testing)")
    ap.add_argument("--state", default="migration_state.json", help="tracks already-imported tasks")
    ap.add_argument("--apply", action="store_true", help="really create chores (default: dry run)")
    ap.add_argument("--inspect", action="store_true", help="print existing labels/chores from DoneTick and exit")
    args = ap.parse_args()

    dt = None
    if args.url and (args.api_key or args.token or (args.username and args.password)):
        dt = DoneTick(args.url, args.token, args.api_key)
        if not args.token and args.username and args.password:
            try:
                dt.login(args.username, args.password)
            except RuntimeError as e:
                if not args.api_key:
                    raise
                print(f"Warning: JWT login failed ({e}). Labels will not work without a JWT (--token).")

    if args.inspect:
        if not dt:
            sys.exit("--inspect needs --url and credentials")
        chores = dt.chores()
        try:
            print("Labels:", json.dumps(dt.labels(), indent=2)[:2000])
        except RuntimeError as e:
            print("Labels: FAILED ->", e)
            print("  (the labels endpoint needs a JWT: add --token or --username/--password)")
        try:
            projs = dt.projects()
            print("\nProjects (create a nested one in the UI to see the parent field):")
            print(json.dumps(projs[:3], indent=2) if projs else "(no projects yet)")
        except RuntimeError as e:
            print("\nProjects: FAILED ->", e)
        print("\nFirst chore (create one manually in the UI first to see the exact shape):")
        print(json.dumps(chores[0], indent=2) if chores else "(no chores yet)")
        return

    if args.wipe:
        if not dt:
            sys.exit("--wipe needs --url and credentials (API key and JWT)")
        if args.wipe == "migrated" and not args.export:
            sys.exit("--wipe migrated needs --export (to know which projects/labels belong to the migration)")
        wipe(dt, args, load_projects(args.export) if args.export else {})
        return

    if not args.export:
        sys.exit("--export is required")
    if args.apply and not (dt and args.user_id):
        sys.exit("--apply needs --url, credentials and --user-id")

    by_id = load_projects(args.export)
    state = {}
    if os.path.exists(args.state):
        with open(args.state) as f:
            state = json.load(f)

    warned = set()
    plan = []  # (payload, label_names, task_id, project_path)
    projects_needed = {}  # path -> info
    wanted_labels = {}  # name -> color

    for p in by_id.values():
        if p.get("is_archived") and not args.include_archived:
            continue
        chain = project_chain(p["id"], by_id, warned)
        paths, acc = [], []
        for proj in chain:
            acc.append(proj["title"])
            paths.append(" / ".join(acc))

        names = []
        if args.project_mode in ("labels", "both"):
            names = [(args.label_prefix + pa, color(pr.get("hex_color"))) for pa, pr in zip(paths, chain)]

        project_path = None
        if args.project_mode in ("projects", "both") and chain and (p.get("tasks") or args.project_parent_field):
            start = 0 if args.project_parent_field else len(chain) - 1
            for i in range(start, len(chain)):
                if paths[i] not in projects_needed:
                    projects_needed[paths[i]] = {
                        "title": chain[i]["title"],
                        "path": paths[i],
                        "parent_path": paths[i - 1] if i > 0 else None,
                        "color": color(chain[i].get("hex_color")),
                        "description": html_to_text(chain[i].get("description")),
                        "depth": i,
                    }
            project_path = paths[-1]

        for t in p.get("tasks") or []:
            if str(t["id"]) in state:
                continue
            if t.get("done") and not args.include_done:
                continue
            due = parse_dt(t.get("due_date"))
            ftype, freq, meta, rolling = map_recurrence(t, due, args.timezone)
            if ftype != "once" and not due:
                due = datetime.now(timezone.utc)  # recurring chores need a start date
                meta["time"] = iso_utc(due)

            label_names = [n for n, _ in names]
            for n, c in names:
                wanted_labels[n] = c
            for lb in t.get("labels") or []:
                label_names.append(lb["title"])
                wanted_labels.setdefault(lb["title"], color(lb.get("hex_color")))

            payload = {
                "name": t["title"],
                "description": build_description(t, args.description_format),
                "isActive": not t.get("done"),
                "isRolling": rolling,
                "frequencyType": ftype,
                "frequency": freq,
                "frequencyMetadata": meta,
                "priority": PRIORITY_MAP.get(int(t.get("priority") or 0), 0),
            }
            if due:
                payload["nextDueDate"] = iso_utc(due)
            plan.append((payload, label_names, t["id"], project_path))

    long_desc = [tid for pl, _, tid, _ in plan if len(pl["description"]) > 5000]
    if long_desc:
        print(f"WARNING: {len(long_desc)} description(s) exceed 5000 characters (tasks {long_desc[:10]}); "
              "DoneTick may reject or truncate them.")

    extra = {}
    if args.private:
        extra["isPrivate"] = True
    for item in args.set:
        if "=" not in item:
            sys.exit(f"--set expects KEY=VALUE, got: {item}")
        k, v = item.split("=", 1)
        try:
            extra[k] = json.loads(v)
        except json.JSONDecodeError:
            extra[k] = v
    for payload, *_ in plan:
        payload.update(extra)

    if args.limit:
        plan = plan[: args.limit]

    print(f"{len(plan)} task(s) to import, {len(wanted_labels)} label(s) needed.")
    if not args.apply:
        if projects_needed:
            print("\nProjects to create/use:")
            for info in sorted(projects_needed.values(), key=lambda i: i["depth"]):
                nm = info["title"] if args.project_parent_field else info["path"]
                par = f"  (parent: {info['parent_path']})" if args.project_parent_field and info["parent_path"] else ""
                print(f"  - {nm}{par}")
        for payload, lbls, tid, ppath in plan[:5]:
            print(f"\n--- Vikunja task #{tid} project={ppath!r} labels={lbls}")
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        if len(plan) > 5:
            print(f"\n... and {len(plan) - 5} more. Dry run only; add --apply to import.")
        return

    # ensure projects exist
    project_ids = {}
    if projects_needed:
        pf = args.project_parent_field
        existing_p = dt.projects()

        def find(name, parent_id):
            for pr in existing_p:
                if pr.get("name") == name and (not pf or (pr.get(pf) or None) == parent_id):
                    return pr
            return None

        for info in sorted(projects_needed.values(), key=lambda i: i["depth"]):
            name = info["title"] if pf else info["path"]
            parent_id = project_ids.get(info["parent_path"]) if (pf and info["parent_path"]) else None
            match = find(name, parent_id)
            if not match:
                body = {"name": name}
                if info["color"]:
                    body["color"] = info["color"]
                if info["description"]:
                    body["description"] = info["description"]
                if pf and parent_id is not None:
                    body[pf] = parent_id
                dt.req("POST", dt.projects_path, body)
                time.sleep(0.1)
                existing_p = dt.projects()
                match = find(name, parent_id)
            if not match:
                sys.exit(f"Project '{name}' not found after creating it. Run --inspect and check the project fields.")
            project_ids[info["path"]] = match["id"]
        print(f"Projects ready ({len(project_ids)}).")

    # ensure labels exist
    existing = {}
    if not args.no_labels:
        try:
            existing = {l["name"]: l["id"] for l in dt.labels()}
            for name, col in wanted_labels.items():
                if name not in existing:
                    body = {"name": name}
                    if col:
                        body["color"] = col
                    dt.req("POST", "/api/v1/labels", body)
                    time.sleep(0.1)
            existing = {l["name"]: l["id"] for l in dt.labels()}
            print(f"Labels ready ({len(existing)} total in DoneTick).")
        except RuntimeError as e:
            sys.exit(f"Label setup failed: {e}\nProvide a JWT (--token or --username/--password), or use --no-labels.")

    ok = 0
    for payload, lbls, tid, ppath in plan:
        if ppath in project_ids:
            payload["projectId"] = project_ids[ppath]
        payload["assignedTo"] = args.user_id
        payload["assignees"] = [{"userId": args.user_id}]
        payload["assignStrategy"] = "random"
        payload["labelsV2"] = [{"id": existing[n]} for n in dict.fromkeys(lbls) if n in existing]
        try:
            dt.req("POST", "/api/v1/chores/", payload)
        except RuntimeError as e:
            print(f"  FAILED #{tid} '{payload['name']}': {e}")
            continue
        state[str(tid)] = True
        ok += 1
        with open(args.state, "w") as f:
            json.dump(state, f)
        print(f"  ok  #{tid} {payload['name']}")
        time.sleep(0.2)
    print(f"Done: {ok}/{len(plan)} imported. Re-running skips tasks already imported ({args.state}).")


if __name__ == "__main__":
    main()
