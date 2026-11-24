"""Render profile stats as SVG cards (dark and light) from the GitHub GraphQL API.

Usage: GITHUB_TOKEN=... python scripts/stats.py [login]
Stdlib only, so the workflow needs no dependencies.
"""

from __future__ import annotations

import json
import os
import sys
import urllib.request
from datetime import date, datetime, timezone
from html import escape
from pathlib import Path

LOGIN = sys.argv[1] if len(sys.argv) > 1 else "BazZziliuS"
OUT = Path(__file__).resolve().parent.parent / "assets"

QUERY = """
query($login: String!, $after: String) {
  user(login: $login) {
    createdAt
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalPullRequestReviewContributions
      totalIssueContributions
      restrictedContributionsCount
      contributionCalendar {
        totalContributions
        weeks { contributionDays { date contributionCount } }
      }
    }
    repositories(ownerAffiliations: OWNER, isFork: false, privacy: PUBLIC,
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

# Config / markup languages that inflate byte counts without saying much.
SKIP_LANGS = {"HTML", "CSS", "SCSS", "MDX", "Batchfile", "PowerShell", "Shell",
              "Dockerfile", "Makefile", "Mustache", "Jinja", "FreeMarker",
              "GLSL", "ShaderLab", "HLSL"}

THEMES = {
    "dark": {
        "text": "#e6edf3", "muted": "#8b949e", "border": "#30363d",
        "track": "#21262d", "bar": "#3fb950", "bar_dim": "#196c2e",
    },
    "light": {
        "text": "#1f2328", "muted": "#59636e", "border": "#d0d7de",
        "track": "#eff2f5", "bar": "#1a7f37", "bar_dim": "#7ee2a8",
    },
}

FONT = "-apple-system, BlinkMacSystemFont, 'Segoe UI', 'Noto Sans', Helvetica, Arial, sans-serif"
MONTHS = "Jan Feb Mar Apr May Jun Jul Aug Sep Oct Nov Dec".split()


def gql(token: str, variables: dict) -> dict:
    req = urllib.request.Request(
        "https://api.github.com/graphql",
        data=json.dumps({"query": QUERY, "variables": variables}).encode(),
        headers={"Authorization": f"bearer {token}", "User-Agent": "profile-stats"},
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        body = json.load(resp)
    if body.get("errors"):
        raise SystemExit(f"GraphQL error: {body['errors']}")
    return body["data"]["user"]


def fetch(token: str) -> dict:
    user = gql(token, {"login": LOGIN})
    repos = list(user["repositories"]["nodes"])
    page = user["repositories"]["pageInfo"]
    while page["hasNextPage"]:
        nxt = gql(token, {"login": LOGIN, "after": page["endCursor"]})
        repos += nxt["repositories"]["nodes"]
        page = nxt["repositories"]["pageInfo"]
    user["repos"] = repos
    return user


def streaks(days: list[dict]) -> tuple[int, int, int]:
    """Return (current, longest, active_days). Today with zero doesn't break the current streak."""
    counts = [d["contributionCount"] for d in days]
    longest = run = 0
    for c in counts:
        run = run + 1 if c else 0
        longest = max(longest, run)
    current = 0
    tail = counts[:-1] if counts and counts[-1] == 0 else counts
    for c in reversed(tail):
        if not c:
            break
        current += 1
    return current, longest, sum(1 for c in counts if c)


def languages(repos: list[dict], top: int = 6) -> list[tuple[str, str, float]]:
    # Each repo gets equal weight, split by its byte mix. Raw bytes would let one
    # repo with vendored or generated code swamp everything else.
    sizes: dict[str, float] = {}
    colors: dict[str, str] = {}
    for repo in repos:
        edges = [e for e in repo["languages"]["edges"] if e["node"]["name"] not in SKIP_LANGS]
        repo_total = sum(e["size"] for e in edges)
        for edge in edges:
            name = edge["node"]["name"]
            sizes[name] = sizes.get(name, 0) + edge["size"] / repo_total
            colors[name] = edge["node"]["color"] or "#8b949e"
    total = sum(sizes.values()) or 1
    ranked = sorted(sizes.items(), key=lambda kv: kv[1], reverse=True)
    head = ranked[:top]
    rest = sum(s for _, s in ranked[top:])
    out = [(n, colors[n], s / total) for n, s in head]
    if rest:
        out.append(("Other", "#8b949e", rest / total))
    return out


def fmt(n: int) -> str:
    return f"{n:,}"


def render(data: dict, theme: str) -> str:
    t = THEMES[theme]
    cc = data["contributionsCollection"]
    cal = cc["contributionCalendar"]
    days = [d for w in cal["weeks"] for d in w["contributionDays"]]
    current, longest, active = streaks(days)
    langs = languages(data["repos"])

    W, PAD = 840, 28
    inner = W - PAD * 2

    tiles = [
        (fmt(cal["totalContributions"]), "contributions, past year"),
        (fmt(active), "active days"),
        (f"{current} d", "current streak"),
        (f"{longest} d", "longest streak"),
        (fmt(cc["totalPullRequestContributions"]), "pull requests"),
    ]

    parts: list[str] = []
    add = parts.append

    # Numbers row
    col = inner / len(tiles)
    for i, (value, label) in enumerate(tiles):
        x = PAD + i * col
        add(f'<text x="{x:.1f}" y="62" class="num">{escape(value)}</text>')
        add(f'<text x="{x:.1f}" y="84" class="lbl">{escape(label)}</text>')

    # Weekly activity
    weeks = [sum(d["contributionCount"] for d in w["contributionDays"]) for w in cal["weeks"]]
    top_y, chart_h = 124, 84
    add(f'<text x="{PAD}" y="{top_y}" class="h">Weekly activity</text>')
    base = top_y + 16 + chart_h
    peak = max(weeks) or 1
    gap = 3
    bw = (inner - gap * (len(weeks) - 1)) / len(weeks)
    add(f'<line x1="{PAD}" y1="{base + 0.5}" x2="{W - PAD}" y2="{base + 0.5}" stroke="{t["border"]}"/>')
    last_month = None
    for i, (w, total) in enumerate(zip(cal["weeks"], weeks)):
        x = PAD + i * (bw + gap)
        h = max(2.0, chart_h * total / peak) if total else 2.0
        fill = t["bar"] if total else t["track"]
        first = w["contributionDays"][0]["date"]
        add(f'<rect x="{x:.2f}" y="{base - h:.2f}" width="{bw:.2f}" height="{h:.2f}" rx="1.5" fill="{fill}">'
            f'<title>Week of {first}: {total}</title></rect>')
        month = date.fromisoformat(first).month
        if month != last_month and i < len(weeks) - 2:
            if last_month is not None:
                add(f'<text x="{x:.1f}" y="{base + 18}" class="lbl">{MONTHS[month - 1]}</text>')
            last_month = month

    # Languages
    ly = base + 58
    add(f'<text x="{PAD}" y="{ly}" class="h">Languages across public repos</text>')
    by = ly + 14
    add(f'<clipPath id="r"><rect x="{PAD}" y="{by}" width="{inner}" height="8" rx="4"/></clipPath>')
    add('<g clip-path="url(#r)">')
    x = PAD
    for name, color, share in langs:
        w = inner * share
        add(f'<rect x="{x:.2f}" y="{by}" width="{w + 0.5:.2f}" height="8" fill="{color}"/>')
        x += w
    add("</g>")
    lx, ly2 = PAD, by + 32
    for name, color, share in langs:
        label = f"{name} {share * 100:.0f}%"
        width = 14 + len(label) * 6.6
        if lx + width > W - PAD:
            lx, ly2 = PAD, ly2 + 22
        add(f'<circle cx="{lx + 4}" cy="{ly2 - 4}" r="4" fill="{color}"/>')
        add(f'<text x="{lx + 14}" y="{ly2}" class="lbl"><tspan class="strong">{escape(name)}</tspan> {share * 100:.0f}%</text>')
        lx += width + 22

    H = ly2 + 26
    updated = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    add(f'<text x="{W - PAD}" y="{H - 10}" class="foot" text-anchor="end">updated {updated}</text>')

    style = f"""
    text {{ font-family: {FONT}; fill: {t["muted"]}; }}
    .num {{ font-size: 26px; font-weight: 600; fill: {t["text"]}; }}
    .lbl {{ font-size: 12px; }}
    .h {{ font-size: 13px; font-weight: 600; fill: {t["text"]}; }}
    .strong {{ fill: {t["text"]}; font-weight: 500; }}
    .foot {{ font-size: 10px; opacity: .7; }}
    """
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{W}" height="{H}" viewBox="0 0 {W} {H}" '
        f'role="img" aria-label="GitHub stats for {LOGIN}">'
        f"<style>{style}</style>"
        f'<rect x="0.5" y="0.5" width="{W - 1}" height="{H - 1}" rx="10" fill="none" stroke="{t["border"]}"/>'
        + "".join(parts)
        + "</svg>\n"
    )


def main() -> None:
    token = os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not token:
        raise SystemExit("Set GITHUB_TOKEN")
    data = fetch(token)
    OUT.mkdir(exist_ok=True)
    for theme in THEMES:
        (OUT / f"stats-{theme}.svg").write_text(render(data, theme), encoding="utf-8")
    print("ok")


if __name__ == "__main__":
    main()
