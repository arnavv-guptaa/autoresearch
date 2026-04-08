#!/Users/arnavgupta/Documents/GitHub/autoresearch/.venv/bin/python3
"""
SEO audit benchmark for Sunwave Technologies blog.

Two scoring systems:
1. Content audit (custom) — word count, headings, internal links, FAQ, GEO signals
2. Lighthouse SEO (Google's own) — meta tags, structured data, crawlability, mobile

Output is both scores plus a combined metric.

Usage:
    python benchmark.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
import urllib.request
import urllib.error

WORKTREE = "/Users/arnavgupta/Documents/arnav/sunwave/web-autoresearch-seo"
BLOG_DIR = os.path.join(WORKTREE, "content/blog")
APP_DIR = os.path.join(WORKTREE, "src/app")
SITE_URL = "https://www.sunwavetech.com"
DEV_PORT = 3002  # port for benchmark dev server (avoid conflicts)
DEV_URL = f"http://localhost:{DEV_PORT}"

# --- Scoring weights ---
# Total possible ~100 per post + technical bonus

def count_words(text):
    """Count words in markdown text (excluding frontmatter)."""
    # Strip frontmatter
    text = re.sub(r'^---.*?---', '', text, flags=re.DOTALL).strip()
    # Strip markdown syntax
    text = re.sub(r'[#*_\[\]()>`|]', ' ', text)
    return len(text.split())


def parse_frontmatter(content):
    """Extract frontmatter as dict."""
    match = re.match(r'^---\s*\n(.*?)\n---', content, re.DOTALL)
    if not match:
        return {}
    fm = {}
    for line in match.group(1).split('\n'):
        if ':' in line:
            key, val = line.split(':', 1)
            fm[key.strip()] = val.strip().strip('"').strip("'")
    return fm


def get_body(content):
    """Get markdown body without frontmatter."""
    return re.sub(r'^---.*?---', '', content, flags=re.DOTALL).strip()


def audit_post(filepath, all_slugs):
    """Audit a single blog post. Returns (score, details)."""
    with open(filepath, 'r') as f:
        content = f.read()

    fm = parse_frontmatter(content)
    body = get_body(content)
    word_count = count_words(content)
    slug = os.path.basename(filepath).replace('.mdx', '')

    score = 0
    details = []

    # --- Content Quality (max 40) ---

    # Word count: 0-40 points scaled (1500+ = full marks)
    wc_score = min(word_count / 1500, 1.0) * 25
    score += wc_score
    details.append(f"words={word_count} ({wc_score:.0f}/25)")

    # Has H2 headings (structure)
    h2_count = len(re.findall(r'^## ', body, re.MULTILINE))
    if h2_count >= 3:
        score += 5
        details.append(f"h2={h2_count} (5/5)")
    else:
        details.append(f"h2={h2_count} (0/5)")

    # Has H3 subheadings (depth)
    h3_count = len(re.findall(r'^### ', body, re.MULTILINE))
    if h3_count >= 2:
        score += 5
        details.append(f"h3={h3_count} (5/5)")
    else:
        details.append(f"h3={h3_count} (0/5)")

    # Lists (scannable content)
    list_items = len(re.findall(r'^[-*]\s', body, re.MULTILINE))
    if list_items >= 5:
        score += 5
        details.append(f"lists={list_items} (5/5)")
    else:
        details.append(f"lists={list_items} (0/5)")

    # --- On-Page SEO (max 25) ---

    # Has title
    if fm.get('title'):
        score += 5
        # Title length (50-60 chars ideal)
        tlen = len(fm['title'])
        if 40 <= tlen <= 70:
            score += 3
            details.append(f"title={tlen}ch (8/8)")
        else:
            details.append(f"title={tlen}ch (5/8)")
    else:
        details.append("title=MISSING (0/8)")

    # Has meta description
    if fm.get('description'):
        dlen = len(fm['description'])
        if 120 <= dlen <= 160:
            score += 7
            details.append(f"desc={dlen}ch (7/7)")
        elif fm['description']:
            score += 4
            details.append(f"desc={dlen}ch (4/7)")
    else:
        details.append("desc=MISSING (0/7)")

    # Has image
    if fm.get('image'):
        score += 5
        details.append("image=yes (5/5)")
    else:
        details.append("image=MISSING (0/5)")

    # Has category
    if fm.get('category'):
        score += 5
        details.append(f"category={fm['category']} (5/5)")
    else:
        details.append("category=MISSING (0/5)")

    # --- Internal Linking (max 10) ---

    # Links to other blog posts
    internal_links = 0
    for other_slug in all_slugs:
        if other_slug != slug and other_slug in body:
            internal_links += 1
    # Also count /blog/ links
    blog_links = len(re.findall(r'\(/blog/', body))
    internal_links = max(internal_links, blog_links)

    il_score = min(internal_links, 3) * 3.33
    score += il_score
    details.append(f"internal_links={internal_links} ({il_score:.0f}/10)")

    # --- GEO / Generative Engine Optimization (max 25) ---

    # FAQ section (critical for GEO — AI models love Q&A format)
    has_faq = bool(re.search(r'(FAQ|Frequently Asked|Common Questions)', body, re.IGNORECASE))
    if has_faq:
        score += 8
        details.append("faq=yes (8/8)")
    else:
        details.append("faq=no (0/8)")

    # Statistics / numbers (AI models cite concrete data)
    stats = re.findall(r'\d+[%₹$,.]?\d*', body)
    if len(stats) >= 5:
        score += 5
        details.append(f"stats={len(stats)} (5/5)")
    else:
        s = min(len(stats), 5) * 1
        score += s
        details.append(f"stats={len(stats)} ({s}/5)")

    # Definitive statements (AI models prefer clear answers)
    # Look for patterns like "X is Y", "The answer is", "This means"
    definitive = len(re.findall(
        r'(the (?:best|most|key|main|primary|answer|result)|this means|in short|to summarize|the bottom line)',
        body, re.IGNORECASE
    ))
    if definitive >= 3:
        score += 5
        details.append(f"definitive={definitive} (5/5)")
    else:
        s = min(definitive, 3) * 1.67
        score += s
        details.append(f"definitive={definitive} ({s:.0f}/5)")

    # Table of contents / summary (GEO: easy extraction)
    has_toc = bool(re.search(r'(table of contents|in this article|what you.ll learn|key takeaways|TL;DR|summary)', body, re.IGNORECASE))
    if has_toc:
        score += 4
        details.append("toc/summary=yes (4/4)")
    else:
        details.append("toc/summary=no (0/4)")

    # Structured comparison (tables, vs sections)
    has_comparison = bool(re.search(r'(\|.*\|.*\||\bvs\.?\b|compared to|comparison)', body, re.IGNORECASE))
    if has_comparison:
        score += 3
        details.append("comparison=yes (3/3)")
    else:
        details.append("comparison=no (0/3)")

    return score, details


def audit_technical():
    """Audit technical SEO elements. Returns (score, details)."""
    score = 0
    details = []

    # sitemap.ts exists
    sitemap_path = os.path.join(APP_DIR, "sitemap.ts")
    if os.path.exists(sitemap_path):
        score += 10
        details.append("sitemap=yes (10/10)")
    else:
        details.append("sitemap=MISSING (0/10)")

    # robots.ts exists
    robots_path = os.path.join(APP_DIR, "robots.ts")
    if os.path.exists(robots_path):
        score += 10
        details.append("robots=yes (10/10)")
    else:
        details.append("robots=MISSING (0/10)")

    # Blog post count (more content = better SEO footprint)
    post_count = len([f for f in os.listdir(BLOG_DIR) if f.endswith('.mdx')])
    pc_score = min(post_count / 20, 1.0) * 30  # 20+ posts = full marks
    score += pc_score
    details.append(f"posts={post_count} ({pc_score:.0f}/30)")

    # Category diversity
    categories = set()
    for f in os.listdir(BLOG_DIR):
        if f.endswith('.mdx'):
            with open(os.path.join(BLOG_DIR, f)) as fh:
                fm = parse_frontmatter(fh.read())
                if fm.get('category'):
                    categories.add(fm['category'])
    cat_score = min(len(categories) / 5, 1.0) * 10  # 5+ categories = full marks
    score += cat_score
    details.append(f"categories={len(categories)} ({cat_score:.0f}/10)")

    return score, details


def check_build():
    """Run next build to ensure site compiles. Returns True/False."""
    result = subprocess.run(
        ["npx", "next", "build"],
        cwd=WORKTREE,
        capture_output=True,
        text=True,
        timeout=120,
    )
    return result.returncode == 0, result.stderr[-500:] if result.returncode != 0 else ""


def start_dev_server():
    """Start Next.js dev server for Lighthouse. Returns process."""
    proc = subprocess.Popen(
        ["npx", "next", "dev", "-p", str(DEV_PORT)],
        cwd=WORKTREE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        preexec_fn=os.setsid,
    )
    # Wait for server to be ready
    for _ in range(30):
        time.sleep(1)
        try:
            req = urllib.request.Request(DEV_URL)
            with urllib.request.urlopen(req, timeout=2):
                return proc
        except (urllib.error.URLError, ConnectionError, OSError):
            continue
    # Server didn't start
    stop_dev_server(proc)
    return None


def stop_dev_server(proc):
    """Kill the dev server and all child processes."""
    try:
        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
    except (ProcessLookupError, OSError):
        pass
    proc.wait(timeout=5)


def run_lighthouse(url):
    """Run Lighthouse SEO audit on a URL. Returns score 0-100 or None."""
    try:
        result = subprocess.run(
            [
                "npx", "lighthouse", url,
                "--only-categories=seo",
                "--output=json",
                "--chrome-flags=--headless --no-sandbox",
                "--quiet",
            ],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            return None
        report = json.loads(result.stdout)
        return report.get("categories", {}).get("seo", {}).get("score", 0) * 100
    except (subprocess.TimeoutExpired, json.JSONDecodeError, KeyError):
        return None


def audit_lighthouse(all_slugs):
    """Run Lighthouse on blog listing + sample of posts. Returns (avg_score, details)."""
    print("\n--- Lighthouse SEO Audit ---")

    server = start_dev_server()
    if not server:
        print("lighthouse: dev server failed to start, skipping")
        return None, ["server_failed"]

    try:
        scores = []
        pages = [("/blog", "blog-listing")]

        # Test up to 5 posts (newest first by filename sort, reversed)
        sample_slugs = sorted(all_slugs, reverse=True)[:5]
        for slug in sample_slugs:
            pages.append((f"/blog/{slug}", slug))

        for path, name in pages:
            url = f"{DEV_URL}{path}"
            score = run_lighthouse(url)
            if score is not None:
                scores.append({"name": name, "score": score})
                print(f"lighthouse: {name} = {score:.0f}/100")
            else:
                print(f"lighthouse: {name} = FAILED")

        if not scores:
            return None, ["all_pages_failed"]

        avg = sum(s["score"] for s in scores) / len(scores)
        return avg, scores

    finally:
        stop_dev_server(server)


def fail(msg):
    print(f"seo_score: 0")
    print(f"lighthouse_avg: 0")
    print(f"status: failed")
    print(f"error: {msg}")
    sys.exit(1)


def main():
    # 1. Check build
    build_ok, build_err = check_build()
    if not build_ok:
        fail(f"next build failed: {build_err}")

    # 2. Get all post slugs
    posts = [f for f in os.listdir(BLOG_DIR) if f.endswith('.mdx')]
    all_slugs = [f.replace('.mdx', '') for f in posts]

    if not posts:
        fail("No blog posts found")

    # 3. Audit each post (content score)
    post_scores = []
    print("--- Post Scores ---")
    for f in sorted(posts):
        filepath = os.path.join(BLOG_DIR, f)
        score, details = audit_post(filepath, all_slugs)
        slug = f.replace('.mdx', '')
        post_scores.append({"slug": slug, "score": score})
        print(f"post: {slug} score={score:.1f}/100 | {', '.join(details)}")

    # 4. Audit technical SEO
    tech_score, tech_details = audit_technical()
    print(f"\n--- Technical SEO ---")
    print(f"technical: score={tech_score:.1f}/60 | {', '.join(tech_details)}")

    # 5. Content composite score
    avg_post_score = sum(p["score"] for p in post_scores) / len(post_scores)
    content_composite = (avg_post_score * 0.6) + (tech_score / 60 * 100 * 0.4)

    # 6. Lighthouse audit
    lighthouse_avg, lighthouse_details = audit_lighthouse(all_slugs)

    # 7. Combined score: 40% content + 60% lighthouse (lighthouse weighted more)
    if lighthouse_avg is not None:
        combined = (content_composite * 0.4) + (lighthouse_avg * 0.6)
    else:
        combined = content_composite  # fallback if lighthouse failed

    # 8. Report
    print(f"\n--- Summary ---")
    print(f"post_count: {len(posts)}")
    print(f"avg_post_score: {avg_post_score:.1f}")
    print(f"technical_score: {tech_score:.1f}")
    print(f"content_score: {content_composite:.1f}")
    print(f"lighthouse_avg: {lighthouse_avg:.1f}" if lighthouse_avg else "lighthouse_avg: N/A")
    print(f"seo_score: {combined:.1f}")
    print(f"status: success")

    # Show weakest posts
    for p in sorted(post_scores, key=lambda x: x["score"]):
        if p["score"] < 70:
            print(f"weak_post: {p['slug']} ({p['score']:.0f}/100)")

    sys.exit(0)


if __name__ == "__main__":
    main()
