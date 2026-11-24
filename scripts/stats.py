"""Render the profile's SVG cards (dark + light) from live data.

Cards:
  overview   numbers, weekly activity, languages
  focus      where the past year's commits went
  hours      when commits happen (weekday x hour)
  shipped    live download / star counts for released projects

It also rewrites the block between the live markers in README.md with
repos pushed to in the last two weeks and the latest blog posts.

Usage: GITHUB_TOKEN=... python scripts/stats.py [login]
Stdlib only. A token that can read private repos (STATS_TOKEN in the
workflow) adds private work to the aggregates; private repo names are
never written out.
"""

from __future__ import annotations

import json
import os
import sys
import re
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from html import escape
from pathlib import Path

LOGIN = sys.argv[1] if len(sys.argv) > 1 else "BazZziliuS"
ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "assets"
CACHE = OUT / "metrics.json"
TZ = timezone(timedelta(hours=int(os.environ.get("TZ_OFFSET", "3"))))
TZ_LABEL = os.environ.get("TZ_LABEL", "UTC+3")
README = ROOT / "README.md"
BLOG_RSS = "https://blog.cloudea.org/rss.xml"

# Released projects shown on the "shipped" card.
SHIPPED = [
    {
        "name": "KillSwitch",
        "what": "Per-app kill switch for Windows",
        "repo": "BazZziliuS/KillSwitch",
        "sources": ["gh_releases"],
    },
    {
        "name": "Mekanism Stack Processor",
        "what": "NeoForge mod, batch processing",
        "repo": "BazZziliuS/Mekanism-Stack-Processor",
        "sources": ["modrinth:mekanism-stack-processor", "curseforge:mekanism-stack-processor"],
    },
    {
        "name": "boostylib",
        "what": "Async Python client for Boosty",
        "repo": "BazZziliuS/boostylib",
        "sources": ["pypi:boostylib"],
    },
]

# Config / markup languages that say little about what someone writes.
SKIP_LANGS = {"HTML", "CSS", "SCSS", "MDX", "Batchfile", "PowerShell", "Shell",
              "Dockerfile", "Makefile", "Mustache", "Jinja", "FreeMarker",
              "GLSL", "ShaderLab", "HLSL"}

THEMES = {
    "dark": {"text": "#e6edf3", "muted": "#8b949e", "border": "#30363d",
             "track": "#21262d", "bar": "#3fb950"},
    "light": {"text": "#1f2328", "muted": "#59636e", "border": "#d0d7de",
              "track": "#eff2f5", "bar": "#1a7f37"},
    # Filled background for hosts that can't switch images by color scheme (GitLab).
    "solid": {"text": "#e6edf3", "muted": "#8b949e", "border": "#30363d",
              "track": "#21262d", "bar": "#3fb950", "bg": "#0d1117"},
}
FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()
WEEKDAYS = "Mon Tue Wed Thu Fri Sat Sun".split()
FULL_W, HALF_W, PAD = 840, 412, 24


# ---------------------------------------------------------------- fetching

def http_json(url: str, token: str | None = None, body: dict | None = None):
    headers = {"User-Agent": "profile-stats", "Accept": "application/json"}
    if token:
        headers["Authorization"] = f"bearer {token}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, headers=headers)
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.load(resp)


def gql(token: str, query: str, **variables):
    body = http_json("https://api.github.com/graphql", token, {"query": query, "variables": variables})
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]


PROFILE_Q = """
query($login: String!, $after: String) {
  user(login: $login) {
    id
    contributionsCollection {
      totalPullRequestContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
      commitContributionsByRepository(maxRepositories: 100) {
        repository { nameWithOwner isPrivate }
        contributions { totalCount }
      }
    }
    repositories(ownerAffiliations: OWNER, isFork: false,
                 first: 100, after: $after) {
      totalCount
      pageInfo { hasNextPage endCursor }
      nodes {
        languages(first: 10, orderBy: {field: SIZE, direction: DESC}) {
          edges { size node { name color } }
        }
      }
    }
  }
}
"""

HISTORY_Q = """
query($owner: String!, $name: String!, $author: ID!, $since: GitTimestamp!, $after: String) {
  repository(owner: $owner, name: $name) {
    defaultBranchRef { target { ... on Commit {
      history(author: {id: $author}, since: $since, first: 100, after: $after) {
        pageInfo { hasNextPage endCursor }
        nodes { authoredDate }
      }
    } } }
  }
}
"""


def fetch_profile(token: str) -> dict:
    user = gql(token, PROFILE_Q, login=LOGIN)["user"]
    repos = list(user["repositories"]["nodes"])
    page = user["repositories"]["pageInfo"]
    while page["hasNextPage"]:
        nxt = gql(token, PROFILE_Q, login=LOGIN, after=page["endCursor"])["user"]
        repos += nxt["repositories"]["nodes"]
        page = nxt["repositories"]["pageInfo"]
    user["repos"] = repos
    return user


VISIBLE_Q = """
query($login: String!, $after: String) {
  user(login: $login) {
    repositories(ownerAffiliations: [OWNER, COLLABORATOR, ORGANIZATION_MEMBER], first: 100,
                 after: $after, orderBy: {field: PUSHED_AT, direction: DESC}) {
      pageInfo { hasNextPage endCursor }
      nodes { nameWithOwner isPrivate pushedAt }
    }
  }
}
"""


def candidate_repos(token: str, user: dict, since: str) -> dict[str, bool]:
    """Repos that may hold the user's commits from the past year -> isPrivate.

    The contributions API only itemises public repos (private work comes back as one
    restricted count), so also walk every repo the token can see, pushed since `since`.
    """
    repos = {i["repository"]["nameWithOwner"]: i["repository"]["isPrivate"]
             for i in user["contributionsCollection"]["commitContributionsByRepository"]}
    after = None
    while True:
        page = gql(token, VISIBLE_Q, login=LOGIN, after=after)["user"]["repositories"]
        for n in page["nodes"]:
            if n["pushedAt"] < since:
                return repos
            repos.setdefault(n["nameWithOwner"], n["isPrivate"])
        if not page["pageInfo"]["hasNextPage"]:
            return repos
        after = page["pageInfo"]["endCursor"]


def fetch_commits(token: str, user: dict) -> tuple[list[datetime], dict[str, tuple[bool, int]]]:
    """Authored commit times on default branches over the past year, plus per-repo counts."""
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%dT%H:%M:%SZ")
    times: list[datetime] = []
    per_repo: dict[str, tuple[bool, int]] = {}
    for full, private in candidate_repos(token, user, since).items():
        owner, name = full.split("/")
        if "backup" in name.lower():  # automated snapshots, not work
            continue
        after, count = None, 0
        while True:
            repo = gql(token, HISTORY_Q, owner=owner, name=name, author=user["id"],
                       since=since, after=after)["repository"]
            ref = repo and repo["defaultBranchRef"]
            if not ref:
                break
            hist = ref["target"]["history"]
            times += [datetime.fromisoformat(n["authoredDate"].replace("Z", "+00:00"))
                      for n in hist["nodes"]]
            count += len(hist["nodes"])
            if not hist["pageInfo"]["hasNextPage"]:
                break
            after = hist["pageInfo"]["endCursor"]
        if count:
            per_repo[full] = (private, count)
    return times, per_repo


CALENDAR_Q = """
query($login: String!) {
  user(login: $login) {
    contributionsCollection {
      totalPullRequestContributions
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
  }
}
"""


def use_public_calendar(user: dict, public_token: str) -> None:
    """Take the calendar as the public profile shows it. A fine-grained token as the
    viewer can see fewer private contributions than an anonymous visitor does."""
    cc = gql(public_token, CALENDAR_Q, login=LOGIN)["user"]["contributionsCollection"]
    mine = user["contributionsCollection"]
    if cc["contributionCalendar"]["totalContributions"] >= mine["contributionCalendar"]["totalContributions"]:
        mine.update(cc)


def pr_count(token: str, fallback: int) -> int:
    """PRs opened in the past year. Search sees private repos the token can read;
    the contributions API leaves them out."""
    since = (datetime.now(timezone.utc) - timedelta(days=365)).strftime("%Y-%m-%d")
    try:
        q = urllib.parse.quote(f"type:pr author:{LOGIN} created:>={since}")
        found = http_json(f"https://api.github.com/search/issues?q={q}&per_page=1", token)["total_count"]
        return max(found, fallback)
    except Exception as exc:
        print(f"warn: pr search: {exc}", file=sys.stderr)
        return fallback


def fetch_source(source: str, repo: str, token: str) -> dict:
    """One external metric. Returns {"downloads": n} and/or other fields."""
    kind, _, arg = source.partition(":")
    if kind == "gh_releases":
        rels = http_json(f"https://api.github.com/repos/{repo}/releases?per_page=100", token)
        return {"downloads": sum(a["download_count"] for r in rels for a in r["assets"])}
    if kind == "modrinth":
        return {"downloads": http_json(f"https://api.modrinth.com/v2/project/{arg}")["downloads"]}
    if kind == "curseforge":
        return {"downloads": http_json(f"https://api.cfwidget.com/minecraft/mc-mods/{arg}")["downloads"]["total"]}
    if kind == "pypi":
        rows = http_json(f"https://pypistats.org/api/packages/{arg}/overall?mirrors=false")["data"]
        return {"downloads": sum(r["downloads"] for r in rows)}
    raise ValueError(source)


def fetch_shipped(token: str) -> list[dict]:
    """Live metrics, falling back to the last good value per source when an API is down."""
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    out = []
    for proj in SHIPPED:
        repo = http_json(f"https://api.github.com/repos/{proj['repo']}", token)
        try:
            rel = http_json(f"https://api.github.com/repos/{proj['repo']}/releases/latest", token)
            version = rel["tag_name"]
        except Exception:
            version = None
        downloads = 0
        for src in proj["sources"]:
            key = f"{src}@{proj['repo']}" if ":" not in src else src
            try:
                value = fetch_source(src, proj["repo"], token)["downloads"]
                cache[key] = value
            except Exception as exc:  # keep the card rendering on a flaky API
                print(f"warn: {src}: {exc}", file=sys.stderr)
                value = cache.get(key, 0)
            downloads += value
        out.append({**proj, "downloads": downloads, "stars": repo["stargazers_count"],
                    "version": version})
    CACHE.write_text(json.dumps(cache, indent=2, sort_keys=True) + "\n")
    return out


# ---------------------------------------------------------------- shaping

def streaks(days: list[dict]) -> tuple[int, int, int]:
    counts = [d["contributionCount"] for d in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    current = 0
    tail = counts[:-1] if counts and counts[-1] == 0 else counts  # today isn't over yet
    for c in reversed(tail):
        if not c:
            break
        current += 1
    return current, longest, sum(1 for c in counts if c)


def languages(repos: list[dict], top: int = 6) -> list[tuple[str, str, float]]:
    # Equal weight per repo, so one repo full of generated code can't swamp the rest.
    sizes: dict[str, float] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        edges = [e for e in repo["languages"]["edges"] if e["node"]["name"] not in SKIP_LANGS]
        total = sum(e["size"] for e in edges)
        for e in edges:
            name = e["node"]["name"]
            sizes[name] = sizes.get(name, 0) + e["size"] / total
            colors[name] = e["node"]["color"] or "#8b949e"
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    out = [(n, colors[n], s / total) for n, s in ranked[:top]]
    rest = sum(s for _, s in ranked[top:])
    if rest:
        out.append(("Other", "#8b949e", rest / total))
    return out


def focus_rows(per_repo: dict[str, tuple[bool, int]], top: int = 5) -> tuple[list[tuple[str, int]], int]:
    """Top public repos by commits, and the private total (names stay hidden)."""
    public = [(n.split("/", 1)[1] if n.startswith(LOGIN + "/") else n, c)
              for n, (private, c) in per_repo.items() if not private]
    public.sort(key=lambda r: r[1], reverse=True)
    return public[:top], sum(c for is_private, c in per_repo.values() if is_private)


def fmt(n: int) -> str:
    return f"{n:,}"


# ---------------------------------------------------------------- drawing

def svg(t: dict, w: int, h: int, body: list[str], label: str) -> str:
    style = f"""
    text {{ font-family: {FONT}; fill: {t["muted"]}; }}
    .num {{ font-size: 24px; font-weight: 600; fill: {t["text"]}; }}
    .lbl {{ font-size: 12px; }}
    .sm {{ font-size: 11px; }}
    .h {{ font-size: 13px; font-weight: 600; fill: {t["text"]}; }}
    .strong {{ fill: {t["text"]}; font-weight: 500; }}
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'role="img" aria-label="{escape(label)}"><style>{style}</style>'
        f'<rect x="0.5" y="0.5" width="{w - 1}" height="{h - 1}" rx="10" fill="{t.get("bg", "none")}" stroke="{t["border"]}"/>'
        + "".join(body) + "</svg>\n"
    )


def card_overview(user: dict, t: dict) -> str:
    cc = user["contributionsCollection"]
    cal = cc["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"]]
    current, longest, active = streaks(days)
    W, inner = FULL_W, FULL_W - PAD * 2
    b: list[str] = []

    tiles = [
        (fmt(cal["totalContributions"]), "contributions, past year"),
        (fmt(active), "active days"),
        (f"{current} d", "current streak"),
        (f"{longest} d", "longest streak"),
        (fmt(user.get("prs", cc["totalPullRequestContributions"])), "pull requests"),
    ]
    col = inner / len(tiles)
    for i, (value, label) in enumerate(tiles):
        x = PAD + i * col
        b.append(f'<text x="{x:.1f}" y="50" class="num">{escape(value)}</text>')
        b.append(f'<text x="{x:.1f}" y="70" class="lbl">{escape(label)}</text>')

    weeks = [sum(d["contributionCount"] for d in w["contributionDays"]) for w in cal["weeks"]]
    chart_h, base = 56, 158
    peak = max(weeks) or 1
    gap = 3
    bw = (inner - gap * (len(weeks) - 1)) / len(weeks)
    last_month = None
    for i, (w, total) in enumerate(zip(cal["weeks"], weeks)):
        x = PAD + i * (bw + gap)
        h = max(2.0, chart_h * total / peak)
        first = w["contributionDays"][0]["date"]
        b.append(f'<rect x="{x:.2f}" y="{base - h:.2f}" width="{bw:.2f}" height="{h:.2f}" rx="1.5" '
                 f'fill="{t["bar"] if total else t["track"]}"><title>Week of {first}: {total}</title></rect>')
        month = date.fromisoformat(first).month
        if month != last_month and i < len(weeks) - 2:
            if last_month is not None:
                b.append(f'<text x="{x:.1f}" y="{base + 16}" class="sm">{MONTHS[month - 1]}</text>')
            last_month = month

    by = base + 36
    b.append(f'<clipPath id="r"><rect x="{PAD}" y="{by}" width="{inner}" height="8" rx="4"/></clipPath><g clip-path="url(#r)">')
    x = PAD
    langs = languages(user["repos"])
    for _, color, share in langs:
        b.append(f'<rect x="{x:.2f}" y="{by}" width="{inner * share + 0.5:.2f}" height="8" fill="{color}"/>')
        x += inner * share
    b.append("</g>")
    lx, ly = PAD, by + 30
    for name, color, share in langs:
        width = 14 + len(f"{name} {share * 100:.0f}%") * 6.6
        if lx + width > W - PAD:
            lx, ly = PAD, ly + 20
        b.append(f'<circle cx="{lx + 4}" cy="{ly - 4}" r="4" fill="{color}"/>')
        b.append(f'<text x="{lx + 14}" y="{ly}" class="lbl"><tspan class="strong">{escape(name)}</tspan> {share * 100:.0f}%</text>')
        lx += width + 22
    return svg(t, W, ly + 22, b, f"GitHub activity for {LOGIN}")


def card_focus(per_repo: dict[str, tuple[bool, int]], t: dict) -> str:
    rows, private = focus_rows(per_repo)
    W, inner = HALF_W, HALF_W - PAD * 2
    b = [f'<text x="{PAD}" y="34" class="h">Where the commits went</text>',
         f'<text x="{W - PAD}" y="34" class="sm" text-anchor="end">past year</text>']
    # Bars compare public repos with each other; the private total sits below as a number.
    peak = max((c for _, c in rows), default=1)
    y = 62
    for name, count in rows:
        b.append(f'<text x="{PAD}" y="{y}" class="lbl"><tspan class="strong">{escape(name)}</tspan></text>')
        b.append(f'<text x="{W - PAD}" y="{y}" class="lbl" text-anchor="end">{fmt(count)}</text>')
        b.append(f'<rect x="{PAD}" y="{y + 6}" width="{inner}" height="4" rx="2" fill="{t["track"]}"/>')
        b.append(f'<rect x="{PAD}" y="{y + 6}" width="{max(4, inner * count / peak):.1f}" height="4" rx="2" '
                 f'fill="{t["bar"]}"/>')
        y += 30
    if private:
        b.append(f'<line x1="{PAD}" y1="{y - 8}" x2="{W - PAD}" y2="{y - 8}" stroke="{t["border"]}"/>')
        b.append(f'<text x="{PAD}" y="{y + 12}" class="lbl">Private repos</text>')
        b.append(f'<text x="{W - PAD}" y="{y + 12}" class="lbl" text-anchor="end">'
                 f'<tspan class="strong">{fmt(private)}</tspan></text>')
    return svg(t, W, 248, b, "Commits per repository over the past year")


def card_hours(times: list[datetime], t: dict) -> str:
    grid = [[0] * 24 for _ in range(7)]
    for ts in times:
        local = ts.astimezone(TZ)
        grid[local.weekday()][local.hour] += 1
    peak = max(max(r) for r in grid) or 1
    W = HALF_W
    left, top, cell = PAD + 30, 54, (HALF_W - PAD * 2 - 30) / 24
    b = [f'<text x="{PAD}" y="34" class="h">When I commit</text>',
         f'<text x="{W - PAD}" y="34" class="sm" text-anchor="end">{fmt(len(times))} commits, {TZ_LABEL}</text>']
    for d, row in enumerate(grid):
        cy = top + d * 22 + 11
        b.append(f'<text x="{PAD}" y="{cy + 4}" class="sm">{WEEKDAYS[d]}</text>')
        for hour, n in enumerate(row):
            cx = left + hour * cell + cell / 2
            if n:
                r = 1.5 + (cell / 2 - 1.5) * n / peak
                b.append(f'<circle cx="{cx:.1f}" cy="{cy}" r="{r:.2f}" fill="{t["bar"]}">'
                         f'<title>{WEEKDAYS[d]} {hour:02d}:00 — {n}</title></circle>')
            else:
                b.append(f'<circle cx="{cx:.1f}" cy="{cy}" r="1" fill="{t["border"]}"/>')
    for hour in (0, 6, 12, 18):
        b.append(f'<text x="{left + hour * cell + cell / 2:.1f}" y="{top + 7 * 22 + 14}" class="sm" '
                 f'text-anchor="middle">{hour:02d}</text>')
    busiest = max(range(24), key=lambda h: sum(grid[d][h] for d in range(7)))
    b.append(f'<text x="{PAD}" y="{top + 7 * 22 + 34}" class="sm">Busiest hour: '
             f'<tspan class="strong">{busiest:02d}:00</tspan></text>')
    return svg(t, W, 248, b, "Commits by weekday and hour")


def card_shipped(projects: list[dict], t: dict) -> str:
    W, inner = FULL_W, FULL_W - PAD * 2
    col = inner / len(projects)
    b = []
    for i, p in enumerate(projects):
        x = PAD + i * col
        if i:
            b.append(f'<line x1="{x - 16:.1f}" y1="22" x2="{x - 16:.1f}" y2="104" stroke="{t["border"]}"/>')
        b.append(f'<text x="{x:.1f}" y="38" class="h">{escape(p["name"])}</text>')
        b.append(f'<text x="{x:.1f}" y="56" class="sm">{escape(p["what"])}</text>')
        b.append(f'<text x="{x:.1f}" y="90" class="num">{fmt(p["downloads"])}</text>')
        meta = ["downloads"]
        if p["version"]:
            meta.append(p["version"])
        if p["stars"]:
            meta.append(f"★ {p['stars']}")
        b.append(f'<text x="{x:.1f}" y="108" class="lbl">{escape(" · ".join(meta))}</text>')
    return svg(t, W, 128, b, "Download counts for released projects")


# ---------------------------------------------------------------- README block

RECENT_Q = """
query($login: String!, $since: GitTimestamp!) {
  user(login: $login) {
    id
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC, isArchived: false,
                 first: 10, orderBy: {field: PUSHED_AT, direction: DESC}) {
      nodes {
        name url description
        defaultBranchRef { target { ... on Commit { history(since: $since) { totalCount } } } }
      }
    }
  }
}
"""


def recent_repos(token: str, limit: int = 3, days: int = 14) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    nodes = gql(token, RECENT_Q, login=LOGIN, since=since)["user"]["repositories"]["nodes"]
    out = []
    for n in nodes:
        ref = n["defaultBranchRef"]
        commits = ref["target"]["history"]["totalCount"] if ref else 0
        if n["name"] != LOGIN and commits:
            out.append({**n, "commits": commits})
    return out[:limit]


def blog_posts(limit: int = 3) -> list[dict]:
    req = urllib.request.Request(BLOG_RSS, headers={"User-Agent": "profile-stats"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        root = ET.fromstring(resp.read())
    posts = []
    for item in root.findall("channel/item")[:limit]:
        when = datetime.strptime(item.findtext("pubDate"), "%a, %d %b %Y %H:%M:%S %Z")
        posts.append({"title": item.findtext("title"), "link": item.findtext("link"),
                      "date": f"{MONTHS[when.month - 1]} {when.year}"})
    return posts


def md(text: str) -> str:
    return escape(text, quote=False).replace("[", "\\[").replace("]", "\\]")


def update_readme(token: str) -> None:
    text = README.read_text(encoding="utf-8")
    start, end = "<!-- live:start -->", "<!-- live:end -->"
    if start not in text:
        return
    left = ["<b>Now building</b>", ""]
    for r in recent_repos(token):
        line = f"- [{r['name']}]({r['url']}) <sub>{r['commits']} commit{'s' if r['commits'] != 1 else ''} in 2 weeks</sub>"
        if r["description"]:
            line += f"<br><sub>{md(r['description'])}</sub>"
        left.append(line)
    right = ["<b>Latest posts</b>", ""]
    try:
        for p in blog_posts():
            right.append(f"- [{md(p['title'])}]({p['link']}) <sub>{p['date']}</sub>")
    except Exception as exc:  # keep the previous list when the feed is down
        print(f"warn: blog: {exc}", file=sys.stderr)
        old = re.search(r"<b>Latest posts</b>\n\n(.*?)\n\n</td>", text, re.S)
        if old:
            right.append(old.group(1))
    block = "\n".join([
        start,
        '<table><tr><td valign="top" width="50%">', "",
        *left, "",
        '</td><td valign="top" width="50%">', "",
        *right, "",
        "</td></tr></table>",
        end,
    ])
    text = re.sub(re.escape(start) + r".*?" + re.escape(end), lambda _: block, text, flags=re.S)
    README.write_text(text, encoding="utf-8")


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN")
    user = fetch_profile(token)
    public_token = os.environ.get("PUBLIC_TOKEN")
    if public_token and public_token != token:
        use_public_calendar(user, public_token)
    user["prs"] = pr_count(token, user["contributionsCollection"]["totalPullRequestContributions"])
    times, per_repo = fetch_commits(token, user)
    shipped = fetch_shipped(token)
    OUT.mkdir(exist_ok=True)
    for theme, t in THEMES.items():
        (OUT / f"overview-{theme}.svg").write_text(card_overview(user, t), encoding="utf-8")
        (OUT / f"focus-{theme}.svg").write_text(card_focus(per_repo, t), encoding="utf-8")
        (OUT / f"hours-{theme}.svg").write_text(card_hours(times, t), encoding="utf-8")
        (OUT / f"shipped-{theme}.svg").write_text(card_shipped(shipped, t), encoding="utf-8")
    update_readme(token)
    print(f"ok: {len(times)} commits, {len(shipped)} projects")


if __name__ == "__main__":
    main()
