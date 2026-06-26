#!/usr/bin/env python3
import argparse
import json
import math
import os
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Semaphore
from urllib.parse import urlparse, urljoin

import requests
from bs4 import BeautifulSoup

BASE_URL = "https://www.examtopics.com"
HTTP_TIMEOUT = 20
MAX_CONCURRENT = 15
REQUESTS_PER_SEC = 2.0
MAX_RETRIES = 3
INITIAL_BACKOFF = 1
BACKOFF_FACTOR = 2
PAGE_SIZE = 100


def _get_template_path():
    try:
        import importlib.resources
        return str(importlib.resources.files("examtopics") / "exam.html")
    except Exception:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        return os.path.join(script_dir, "exam.html")


TEMPLATE_PATH = _get_template_path()


def fetch_url(url, session=None):
    if session is None:
        session = requests.Session()
    backoff = INITIAL_BACKOFF
    for attempt in range(MAX_RETRIES + 1):
        if attempt > 0:
            delay = backoff + (time.time() % 0.5)
            print(f"  Retry {attempt} for {url} after {delay:.1f}s", file=sys.stderr)
            time.sleep(delay)
            backoff *= BACKOFF_FACTOR
        try:
            resp = session.get(url, timeout=HTTP_TIMEOUT,
                               headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"})
            if resp.status_code == 200:
                return resp
            if 500 <= resp.status_code < 600:
                print(f"  HTTP {resp.status_code} for {url}, will retry", file=sys.stderr)
                continue
            print(f"  HTTP {resp.status_code} for {url}", file=sys.stderr)
            return None
        except requests.RequestException as e:
            print(f"  Request failed (attempt {attempt}): {e}", file=sys.stderr)
    print(f"  Exhausted retries for {url}", file=sys.stderr)
    return None


def parse_html(url, session=None):
    resp = fetch_url(url, session)
    if resp is None:
        return None
    return BeautifulSoup(resp.text, "html.parser")


def clean_text(raw):
    if not raw:
        return ""
    raw = re.sub(r'\s+', ' ', raw).strip()
    raw = raw.replace("Suggested Answer", "\nSuggested Answer")
    raw = raw.replace("Forgot my password", "")
    return raw


def detect_captcha(soup):
    if soup.find(string=re.compile(r"Enter Captcha", re.I)):
        return True
    if soup.find("button", class_="g-recaptcha"):
        return True
    return False


def parse_url(user_url):
    parsed = urlparse(user_url)
    path = parsed.path.rstrip("/")
    parts = path.split("/")

    result = {
        "type": None,
        "provider": None,
        "exam_slug": None,
        "question_id": None,
        "page": None,
    }

    if "/discussions/" in path and "/view/" in path:
        result["type"] = "discussion_single"
        idx = parts.index("discussions")
        if idx + 1 < len(parts):
            result["provider"] = parts[idx + 1]
        if "/view/" in path:
            vidx = parts.index("view")
            if vidx + 1 < len(parts):
                qid_match = re.match(r'(\d+)', parts[vidx + 1])
                if qid_match:
                    result["question_id"] = qid_match.group(1)
    elif "/discussions/" in path:
        result["type"] = "discussion_list"
        idx = parts.index("discussions")
        if idx + 1 < len(parts):
            result["provider"] = parts[idx + 1]
    elif "/exams/" in path and "/view/" in path:
        result["type"] = "exam_view"
        idx = parts.index("exams")
        if idx + 1 < len(parts):
            result["provider"] = parts[idx + 1]
        if idx + 2 < len(parts):
            result["exam_slug"] = parts[idx + 2]
        vidx = parts.index("view")
        if vidx + 1 < len(parts):
            try:
                result["page"] = int(parts[vidx + 1])
            except ValueError:
                pass
    elif "/exams/" in path:
        result["type"] = "exam_list"
        idx = parts.index("exams")
        if idx + 1 < len(parts):
            result["provider"] = parts[idx + 1]
        if idx + 2 < len(parts):
            result["exam_slug"] = parts[idx + 2]
    return result


def get_max_pages(base_url, session):
    soup = parse_html(base_url, session)
    if soup is None:
        return 1
    page_indicator = soup.select_one(".discussion-list-page-indicator")
    if page_indicator:
        strong_tags = page_indicator.find_all("strong")
        if len(strong_tags) >= 2:
            try:
                return int(strong_tags[1].get_text(strip=True))
            except ValueError:
                pass
        text = page_indicator.get_text()
        m = re.search(r'of\s+(\d+)', text)
        if m:
            return int(m.group(1))
    return 1


def normalize(s):
    return s.lower().replace("-", " ").replace("  ", " ").strip()

def text_matches(href, text, grep_strs):
    if not grep_strs:
        return True
    nh = normalize(href)
    nt = normalize(text)
    for g in grep_strs:
        ng = normalize(g)
        if ng in nh or ng in nt:
            return True
    return False

def get_links_from_page(page_url, grep_strs, session):
    soup = parse_html(page_url, session)
    if soup is None:
        return []
    if isinstance(grep_strs, str):
        grep_strs = [grep_strs] if grep_strs else []
    links = []
    for a_tag in soup.find_all("a", href=True):
        href = a_tag["href"]
        if "/discussions/" in href and "/view/" in href:
            if text_matches(href, a_tag.get_text(), grep_strs):
                links.append(href)
    return links


def get_discussion_links(provider, grep_str, session, max_pages=None, limit_pages=None):
    base_url = f"{BASE_URL}/discussions/{provider}/"
    if max_pages is None:
        max_pages = get_max_pages(base_url, session)
    if limit_pages and limit_pages < max_pages:
        max_pages = limit_pages
    print(f"Scanning {max_pages} pages for provider '{provider}'", file=sys.stderr)

    all_links = []
    sem = Semaphore(MAX_CONCURRENT)
    rate_limit = 1.0 / REQUESTS_PER_SEC
    last_request = [0.0]

    def process_page(page_num):
        with sem:
            now = time.time()
            wait = rate_limit - (now - last_request[0])
            if wait > 0:
                time.sleep(wait)
            last_request[0] = time.time()
            url = f"{BASE_URL}/discussions/{provider}/{page_num}/" if page_num > 1 else base_url
            links = get_links_from_page(url, grep_str, session)
            return links

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = {executor.submit(process_page, i): i for i in range(1, max_pages + 1)}
        done = 0
        for future in as_completed(futures):
            done += 1
            print(f"  Page {done}/{max_pages}", file=sys.stderr, end="\r")
            try:
                links = future.result()
                all_links.extend(links)
            except Exception as e:
                print(f"  Error on page {futures[future]}: {e}", file=sys.stderr)
    print(file=sys.stderr)

    unique = list(dict.fromkeys(all_links))
    sorted_links = sort_links_by_number(unique)
    print(f"Found {len(sorted_links)} unique matching links", file=sys.stderr)
    return sorted_links


def sort_links_by_number(links):
    def extract_nums(url):
        topic_m = re.search(r'topic[-\s]?(\d+)', url, re.I)
        question_m = re.search(r'question[-\s]?(\d+)', url, re.I)
        topic = int(topic_m.group(1)) if topic_m else 0
        question = int(question_m.group(1)) if question_m else 0
        return (topic, question)

    return sorted(links, key=extract_nums)


def get_answer_from_soup(soup):
    correct = soup.select_one(".correct-answer")
    if correct:
        text = correct.get_text(strip=True).replace(" ", "").replace("\n", "")
        if text:
            return text[0]

    for li in soup.select("li.multi-choice-item"):
        if "correct-hidden" in li.get("class", []):
            letter_span = li.select_one(".multi-choice-letter")
            if letter_span:
                m = re.search(r'([A-Z])', letter_span.get_text())
                if m:
                    return m.group(1)

    tally_script = soup.select_one(".voted-answers-tally script")
    if tally_script:
        try:
            data = json.loads(tally_script.string)
            most_voted = None
            most_count = -1
            for entry in data:
                if entry.get("is_most_voted"):
                    return entry.get("voted_answers", "")
                cnt = entry.get("vote_count", 0)
                if cnt > most_count:
                    most_count = cnt
                    most_voted = entry.get("voted_answers", "")
            if most_voted:
                return most_voted
        except (json.JSONDecodeError, AttributeError):
            pass

    comments = soup.select(".comment-selected-answers")
    answer_votes = {}
    for c in comments:
        strong = c.find("strong")
        if strong:
            ans = strong.get_text(strip=True)
            answer_votes[ans] = answer_votes.get(ans, 0) + 1
    if answer_votes:
        return max(answer_votes, key=answer_votes.get)

    return ""


def scrape_question(url, session):
    soup = parse_html(url, session)
    if soup is None:
        return None
    if detect_captcha(soup):
        print(f"  CAPTCHA detected on {url}, skipping", file=sys.stderr)
        return None

    title_tag = soup.find("h1")
    title = clean_text(title_tag.get_text()) if title_tag else ""

    header_tag = soup.select_one(".question-discussion-header")
    header = header_tag.get_text("\n", strip=True) if header_tag else ""

    card_text = soup.select_one("p.card-text")
    content = clean_text(card_text.get_text()) if card_text else ""

    image_url = ""
    exhibit_urls = []
    answer_exhibit_urls = []
    card = soup.select_one("p.card-text")
    if card:
        in_answer_section = False
        answer_markers = ["hot area:", "answer area:", "correct answer:"]
        for child in card.children:
            if hasattr(child, 'name') and child.name == "img" and child.get("src"):
                src = child["src"]
                if src.startswith("//"):
                    src = "https:" + src
                elif not src.startswith("http"):
                    src = urljoin(url, src)
                if in_answer_section:
                    answer_exhibit_urls.append(src)
                else:
                    if not image_url:
                        image_url = src
                    exhibit_urls.append(src)
            elif hasattr(child, 'get_text'):
                child_text = child.get_text().strip().lower()
                if any(m in child_text for m in answer_markers):
                    in_answer_section = True
    if not image_url:
        img_tag = soup.select_one("p.card-text img") or soup.select_one(".question-body img")
        if img_tag and img_tag.get("src"):
            image_url = img_tag["src"]
            if not image_url.startswith("http"):
                image_url = urljoin(url, image_url)

    all_questions = []
    for li in soup.select("li.multi-choice-item"):
        text = clean_text(li.get_text())
        for img in li.find_all("img"):
            src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                elif not src.startswith("http"):
                    src = urljoin(url, src)
                marker = f"[[IMG:{src}]]"
                text = (text + " " + marker).strip() if text else marker
        all_questions.append(text)

    answer = get_answer_from_soup(soup)

    correct_answer_images = []
    correct_box = soup.select_one(".correct-answer")
    if correct_box:
        for img in correct_box.find_all("img"):
            src = img.get("src")
            if src:
                if src.startswith("//"):
                    src = "https:" + src
                elif not src.startswith("http"):
                    src = urljoin(url, src)
                correct_answer_images.append(src)

    meta = soup.select_one(".discussion-meta-data > i")
    timestamp = clean_text(meta.get_text()) if meta else ""

    comments_container = soup.select_one(".discussion-container")
    comments_text = ""
    if comments_container:
        for comment in comments_container.select(".comment-container"):
            username_tag = comment.select_one(".comment-username")
            username = clean_text(username_tag.get_text()) if username_tag else "Anonymous"
            date_tag = comment.select_one(".comment-date")
            date = clean_text(date_tag.get_text()) if date_tag else ""
            body_tag = comment.select_one(".comment-content")
            body = clean_text(body_tag.get_text()) if body_tag else ""
            selected = comment.select_one(".comment-selected-answers")
            selected_text = ""
            if selected:
                st = selected.get_text(" ", strip=True)
                selected_text = f" [{st}]"
            if body:
                comments_text += f"[{username}] ({date}){selected_text}: {body}\n\n"

    return {
        "title": title,
        "header": header,
        "content": content,
        "image": image_url,
        "exhibit_urls": exhibit_urls,
        "answer_exhibit_urls": answer_exhibit_urls,
        "correct_answer_images": correct_answer_images,
        "questions": all_questions,
        "answer": answer,
        "timestamp": timestamp,
        "question_link": url,
        "comments": comments_text.strip(),
    }


def scrape_questions_concurrently(links, session):
    all_data = []
    sem = Semaphore(MAX_CONCURRENT)
    rate_limit = 1.0 / REQUESTS_PER_SEC
    last_request = [0.0]

    def process(link):
        with sem:
            now = time.time()
            wait = rate_limit - (now - last_request[0])
            if wait > 0:
                time.sleep(wait)
            last_request[0] = time.time()
            full_url = link if link.startswith("http") else urljoin(BASE_URL, link)
            return scrape_question(full_url, session)

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
        futures = {executor.submit(process, link): link for link in links}
        done = 0
        total = len(links)
        for future in as_completed(futures):
            done += 1
            print(f"  Question {done}/{total}", file=sys.stderr, end="\r")
            try:
                data = future.result()
                if data and data["title"]:
                    all_data.append(data)
            except Exception as e:
                print(f"  Error: {e}", file=sys.stderr)
    print(file=sys.stderr)
    return all_data


def parse_comments_json(raw_comments):
    if not raw_comments:
        return []
    results = []
    blocks = re.split(r'\n\s*\n', raw_comments)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        user = "Anonymous"
        answer = ""
        text = block

        m = re.match(r'\[([^\]]+)\]\s*(?:\(([^)]*)\))?', block)
        if m:
            user = m.group(1)
            rest = block[m.end():]
            m2 = re.search(r'\[Selected Answer:\s*([^\]]+)\]', rest)
            if m2:
                answer = m2.group(1).strip()
                rest = rest[:m2.start()] + rest[m2.end():]
            text = rest.strip().lstrip(":").strip()

        results.append({"user": user, "answer": answer, "text": text})
    return results


def generate_html(data_list, template_path, provider="", exam_name=""):
    with open(template_path, "r", encoding="utf-8") as f:
        template = f.read()

    display_name = (exam_name.replace("-", " ").title() if exam_name else
                    provider.title() if provider else "Exam")

    template = template.replace("<title>AWS EXAM</title>", f"<title>{display_name}</title>")
    template = template.replace(">AWS EXAM<", f">{display_name}<")

    questions_html = _build_question_cards(data_list)

    start_marker = '\n    <!-- Question 1 -->'
    end_marker = '\n  </div>\n\n  <!-- SUBMIT -->'
    start_idx = template.find(start_marker)
    end_idx = template.find(end_marker)
    if start_idx != -1 and end_idx != -1:
        template = template[:start_idx] + '\n' + questions_html + '\n' + template[end_idx:]

    template = _replace_js_data(template, data_list)

    return template


def _build_question_cards(data_list):
    cards = []
    for i, data in enumerate(data_list):
        if not data["title"]:
            continue
        q_num = i + 1
        q_text = data["content"].strip() or data["title"]
        q_text = q_text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

        letters = []
        for q_item in data.get("questions", []):
            m = re.match(r'\*\*([A-F]):?\*\*\s*(.*)', q_item, re.DOTALL)
            if m:
                letters.append(m.group(1))
            else:
                m = re.match(r'([A-F])[\.\)]\s*(.*)', q_item, re.DOTALL)
                if m:
                    letters.append(m.group(1))

        image_html = ""
        image_url = data.get("image", "")
        if image_url:
            img_src = image_url.replace("&", "&amp;").replace('"', "&quot;")
            image_html = f'''
        <img src="{img_src}" alt="Question image" loading="lazy" decoding="async"
          class="question-image w-full rounded-[var(--radius)] border border-[var(--border)] object-cover max-h-60"
          data-full="{img_src}">'''

        options_html = ""
        question_items = data.get("questions", [])
        if question_items:
            for lidx, q_item in enumerate(question_items):
                letter = letters[lidx] if lidx < len(letters) else chr(65 + lidx)
                clean_opt = q_item
                for pat in [rf'\*\*{letter}:?\*\*\s*', rf'{letter}[\.\)]\s*', rf'<[^>]+>']:
                    clean_opt = re.sub(pat, '', clean_opt, count=1 if pat.startswith(rf'\*\*{letter}') else 0)

                img_markers = re.findall(r'\[\[IMG:([^\]]+)\]\]', clean_opt)
                clean_opt = re.sub(r'\[\[IMG:[^\]]+\]\]', '', clean_opt).strip()
                clean_opt = clean_opt.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

                option_body = f'<span class="text-sm font-medium">{letter}.</span>'
                if clean_opt:
                    option_body += f'<span class="text-sm">{clean_opt}</span>'
                for img_url in img_markers:
                    img_esc = img_url.replace("&", "&amp;").replace('"', "&quot;")
                    option_body += f'<br><img src="{img_esc}" loading="lazy" decoding="async" class="opt-image max-w-full rounded mt-1" style="cursor:pointer">'

                options_html += f'''
            <label class="answer-radio flex flex-col items-start gap-2 rounded-[var(--radius)] border border-[var(--border)] px-4 py-3 cursor-pointer hover:bg-[var(--accent)]/50 transition-colors has-[:checked]:border-[var(--primary)] has-[:checked]:bg-[var(--primary)]/10" data-question="{q_num}" data-answer="{letter.lower()}">
              <div class="flex items-center gap-3">
                <input type="radio" name="q{q_num}" value="{letter.lower()}" class="peer">
                {option_body}
              </div>
            </label>'''
        else:
            correct_imgs = data.get("correct_answer_images", [])
            answer_exhs = data.get("answer_exhibit_urls", [])
            if correct_imgs:
                imgs_html = "".join(
                    f'<img src="{(u.replace("&", "&amp;").replace(chr(34), "&quot;"))}" loading="lazy" decoding="async" class="w-full rounded-[var(--radius)] border border-[var(--border)]" style="cursor:pointer">'
                    for u in correct_imgs
                )
                options_html = f'''
            <div class="answer-block rounded-[var(--radius)] border-2 p-4" style="border-color:color-mix(in oklch, var(--primary) 40%, transparent);background:color-mix(in oklch, var(--primary) 8%, transparent)">
              <p class="text-xs font-semibold mb-2" style="color:var(--primary)">Correct Answer</p>
              {imgs_html}
            </div>'''
            elif answer_exhs:
                imgs_html = "".join(
                    f'<img src="{(u.replace("&", "&amp;").replace(chr(34), "&quot;"))}" loading="lazy" decoding="async" class="w-full rounded-[var(--radius)] border border-[var(--border)]" style="cursor:pointer">'
                    for u in answer_exhs
                )
                options_html = f'''
            <div class="answer-block rounded-[var(--radius)] border-2 p-4" style="border-color:color-mix(in oklch, var(--destructive) 40%, transparent);background:color-mix(in oklch, var(--destructive) 6%, transparent)">
              <p class="text-xs font-semibold mb-1" style="color:var(--destructive)">Answer Area (unverified)</p>
              <p class="text-xs mb-2" style="color:var(--muted-foreground);font-size:11px">Canonical solution not freely available from ExamTopics.</p>
              {imgs_html}
            </div>'''

        link = data.get("question_link", "")
        link_escaped = link.replace("&", "&amp;").replace('"', "&quot;")

        page_num = ((q_num - 1) // PAGE_SIZE) + 1
        cards.append(f'''    <!-- Question {q_num} -->
    <div id="questionCard{q_num}" class="question-card rounded-[var(--radius)] border border-[var(--border)] bg-[var(--card)] text-[var(--card-foreground)] shadow-sm" data-question="{q_num}" data-page="{page_num}" style="content-visibility:auto">
      <div class="p-5 space-y-4">
        <div class="flex items-start justify-between gap-2">
          <div>
            <div class="flex items-center gap-2 flex-wrap">
              <span class="inline-flex items-center rounded-[var(--radius)] border border-[var(--border)] bg-[var(--muted)] text-[var(--muted-foreground)] px-2.5 py-0.5 text-xs font-semibold">Question {q_num}</span>
              <span class="q-result-badge hidden text-xs font-semibold px-2.5 py-0.5 rounded-[var(--radius)]" data-question="{q_num}"></span>
            </div>
            <p class="mt-2 text-sm font-medium leading-relaxed">{q_text}</p>
          </div>
          <button class="reset-question-btn inline-flex items-center justify-center rounded-[var(--radius)] border border-[var(--border)] bg-[var(--secondary)] text-[var(--secondary-foreground)] h-8 w-8 text-sm hover:bg-[var(--accent)] transition-colors flex-shrink-0" title="Reset question" data-question="{q_num}">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><polyline points="1 4 1 10 7 10"/><path d="M3.51 15a9 9 0 1 0 2.13-9.36L1 10"/></svg>
          </button>
        </div>{image_html}
        <div class="space-y-2.5">{options_html}
        </div>
        <div class="flex items-center gap-2">
          <button class="peek-btn inline-flex items-center gap-1.5 rounded-[var(--radius)] border border-[var(--border)] bg-[var(--secondary)] text-[var(--secondary-foreground)] h-8 px-3 text-xs font-medium hover:bg-[var(--accent)] transition-colors" data-question="{q_num}">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
            Peek
          </button>
          <button class="comment-btn inline-flex items-center gap-1.5 rounded-[var(--radius)] border border-[var(--border)] bg-transparent text-[var(--muted-foreground)] h-8 px-3 text-xs font-medium hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)] transition-colors" data-question="{q_num}">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/></svg>
            Comments
          </button>
          <button class="source-btn inline-flex items-center gap-1.5 rounded-[var(--radius)] border border-[var(--border)] bg-transparent text-[var(--muted-foreground)] h-8 px-3 text-xs font-medium hover:bg-[var(--accent)] hover:text-[var(--accent-foreground)] transition-colors" data-url="{link_escaped}">
            <svg xmlns="http://www.w3.org/2000/svg" width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
            Source
          </button>
        </div>
      </div>
    </div>''')

    return '\n'.join(cards)


def _replace_js_data(template, data_list):
    total = len(data_list)
    page_count = math.ceil(total / PAGE_SIZE) if total else 1

    correct_parts = []
    graded_parts = []
    answered_parts = []
    comments_obj = {}

    for i, data in enumerate(data_list):
        q = i + 1
        answer = data.get("answer", "").strip().lower()
        correct_parts.append(f"'{q}': '{answer}'")
        graded_parts.append(f"'{q}': false")
        answered_parts.append(f"'{q}': false")

        raw_comments = data.get("comments", "")
        if raw_comments:
            parsed = parse_comments_json(raw_comments)
            comments_obj[str(q)] = [
                {"author": c["user"], "text": c["text"], "answer": c.get("answer", "")}
                for c in parsed
            ]
        else:
            comments_obj[str(q)] = []

    template = template.replace('var totalQuestions = 3;', f'var totalQuestions = {total};')
    template = template.replace(
        "  var correctAnswers = { '1': 'b', '2': 'c', '3': 'b' };",
        "  var correctAnswers = { " + ", ".join(correct_parts) + " };"
    )
    template = template.replace(
        "  var gradedQuestions = { '1': false, '2': false, '3': false };",
        "  var gradedQuestions = { " + ", ".join(graded_parts) + " };"
    )
    template = template.replace(
        "  var answeredQuestions = { '1': false, '2': false, '3': false };",
        "  var answeredQuestions = { " + ", ".join(answered_parts) + " };"
    )
    template = template.replace('var pageSize = 100;', f'var pageSize = {PAGE_SIZE};')
    template = template.replace('var pageCount = 1;', f'var pageCount = {page_count};')
    template = template.replace('var currentPage = 1;', 'var currentPage = 1;')

    start = template.find('var commentsData = {')
    end = template.find('  };', start)
    if start != -1 and end != -1:
        end += 4
        comments_str = "  var commentsData = " + json.dumps(comments_obj, ensure_ascii=False, indent=4) + ";"
        template = template[:start] + comments_str + template[end:]

    return template


def md_to_text(md_text):
    text = md_text
    text = re.sub(r'(?m)^#{1,6}\s*', '', text)
    text = re.sub(r'(\*\*|\*|__|_)', '', text)
    text = re.sub(r'\[(.*?)\]\(.*?\)', r'\1', text)
    text = re.sub(r'!\[.*?\]\(.*?\)', '', text)
    return text


def write_output(data_list, output_path, include_comments=False, extra_type="html", provider="", exam_name=""):
    base = output_path.rsplit(".", 1)[0] if "." in output_path else output_path
    md_path = base + ".md"

    with open(md_path, "w", encoding="utf-8") as f:
        f.write("# Exam Topics Questions\n\n")

        for data in data_list:
            if not data["title"]:
                continue

            f.write(f"## {data['title']}\n\n")

            if data["header"]:
                f.write(f"{data['header']}\n\n")

            if data["content"]:
                f.write(f"{data['content']}\n\n")

            for q in data["questions"]:
                f.write(f"{q}\n\n")

            if data["answer"]:
                f.write(f"**Answer: {data['answer']}**\n\n")

            if data["timestamp"]:
                f.write(f"**Timestamp: {data['timestamp']}**\n\n")

            f.write(f"[View on ExamTopics]({data['question_link']})\n\n")

            if include_comments and data["comments"]:
                f.write(f"### Comments\n\n{data['comments']}\n\n")

            f.write("----------------------------------------\n\n")

    html_path = base + ".html"
    if os.path.exists(TEMPLATE_PATH):
        display_name = exam_name.replace("-", " ").title() if exam_name else ""
        html_content = generate_html(data_list, TEMPLATE_PATH, provider, display_name)
    else:
        html_content = f"<html><body><pre>Interactive HTML template not found.<br>Place exam.html next to the script.<br><br>Fallback markdown saved at {md_path}</pre></body></html>"

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"Wrote {html_path}")

    keep_md = extra_type == "md"
    if extra_type and extra_type != "html" and not keep_md:
        extra_path = base + "." + extra_type
        with open(md_path, "r", encoding="utf-8") as f:
            content = f.read()
        if extra_type == "txt":
            text = md_to_text(content)
            with open(extra_path, "w", encoding="utf-8") as f:
                f.write(text)
            print(f"Wrote {extra_path}")

    if not keep_md:
        os.remove(md_path)


def scrape_exam_via_discussions(provider, exam_slug, session, limit_pages=None):
    m = re.search(r'([a-zA-Z]+(?:-[a-zA-Z]+\d+)?)$', exam_slug)
    exam_code = m.group(1) if m else exam_slug.split("-")[-1]
    base_code = re.sub(r'-[a-zA-Z]+\d+$', '', exam_code)

    grep_strs = [exam_slug]
    if any(c.isdigit() for c in exam_code):
        grep_strs.extend([exam_code, base_code, exam_code.replace("-", "")])
    grep_strs = [g for g in grep_strs if g and len(g) >= 2]
    grep_strs = list(dict.fromkeys(grep_strs))

    print(f"Searching discussions for '{provider}' matching {grep_strs}...", file=sys.stderr)
    links = get_discussion_links(provider, grep_strs, session, limit_pages=limit_pages)
    return links


DESCRIPTION = """
ExamTopics Downloader -- scrapes exam questions from examtopics.com
and produces an interactive HTML study page.

Output files (written to CWD unless -o is set):
  <base>.html   interactive study page (always)
  <base>.md     kept when -t md
  <base>.txt    added when -t txt

Modes (auto-detected from URL, or use -p/-s):
  /discussions/<provider>/view/<id>-...   single question
  /discussions/<provider>/                all discussions
  /exams/<provider>/<slug>/view/<n>/      exam view
  /exams/<provider>/<slug>/               exam listing
  -p PROVIDER [-s FILTER]                 search by name
"""

EPILOG = """
Examples:

  By provider + exam code (most common):
    examtopics -p amazon -s SAA-C03
    examtopics -p amazon -s SAA-C03 -n 3 -o my-saa

  By URL:
    examtopics https://www.examtopics.com/discussions/amazon/
    examtopics https://www.examtopics.com/exams/amazon/aws-certified-solutions-architect-associate-saa-c03/view/1/

  With comments or extra formats:
    examtopics -p amazon -s SAA-C03 -c -t md
"""


def main():
    parser = argparse.ArgumentParser(
        description=DESCRIPTION,
        epilog=EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    input_group = parser.add_argument_group("Input")
    input_group.add_argument(
        "url", nargs="?",
        help="ExamTopics page URL (discussion, exam view, or exam list). Omit to use -p/-s."
    )
    input_group.add_argument(
        "-p", "--provider",
        help="Provider slug, e.g. amazon, microsoft, google. Use with -s."
    )
    input_group.add_argument(
        "-s", "--search",
        help="Filter discussions by substring (e.g. exam code SAA-C03)."
    )
    scraping_group = parser.add_argument_group("Scraping")
    scraping_group.add_argument(
        "-n", "--pages",
        type=int,
        help="Max discussion listing pages to scan (default: all)."
    )
    output_group = parser.add_argument_group("Output")
    output_group.add_argument(
        "-o", "--output",
        default=None,
        help="Output file base name (default: exam slug from URL, or 'examtopics_output')."
    )
    output_group.add_argument(
        "-c", "--comments",
        action="store_true",
        help="Include user comments from the discussion thread (default: off)."
    )
    output_group.add_argument(
        "-t", "--type",
        default="html", choices=["html", "md", "txt"],
        help="Extra output format alongside HTML: 'md' keeps markdown, 'txt' is plain text (default: html)."
    )
    args = parser.parse_args()

    session = requests.Session()
    session.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    })

    output_path = args.output
    if output_path is None:
        base_name = "examtopics_output"
        if args.url:
            parsed = urlparse(args.url)
            parts = parsed.path.strip("/").split("/")
            if "exams" in parts:
                idx = parts.index("exams")
                if idx + 2 < len(parts):
                    base_name = parts[idx + 2]
            elif "discussions" in parts:
                idx = parts.index("discussions")
                if idx + 1 < len(parts):
                    base_name = parts[idx + 1]
        output_path = base_name

    base = output_path.rsplit(".", 1)[0] if "." in output_path else output_path
    display_path = base + ".html"
    if args.type != "html":
        display_path += " + " + base + "." + args.type

    if args.url:
        info = parse_url(args.url)
        print(f"Detected URL type: {info['type']}", file=sys.stderr)

        if info["type"] == "discussion_single":
            provider = info["provider"]
            qid = info["question_id"]
            if qid:
                print(f"Scraping single question #{qid} from {provider}...", file=sys.stderr)
                data = scrape_question(args.url, session)
                if data:
                    write_output([data], output_path, args.comments, args.type, provider=provider, exam_name=base)
                    print(f"Saved to {display_path}")
                else:
                    print("Failed to scrape question", file=sys.stderr)
                    sys.exit(1)
            else:
                print("Could not extract question ID from URL", file=sys.stderr)
                sys.exit(1)

        elif info["type"] == "discussion_list":
            provider = info["provider"]
            grep_str = args.search or ""
            print(f"Scraping all discussions for '{provider}'...", file=sys.stderr)
            links = get_discussion_links(provider, grep_str, session, limit_pages=args.pages)
            print(f"Scraping {len(links)} questions...", file=sys.stderr)
            data = scrape_questions_concurrently(links, session)
            write_output(data, output_path, args.comments, args.type, provider=provider, exam_name=base)
            print(f"Saved {len(data)} questions to {display_path}")

        elif info["type"] == "exam_view":
            provider = info["provider"]
            exam_slug = info["exam_slug"]
            grep_str = args.search or ""
            if not grep_str and exam_slug:
                links = scrape_exam_via_discussions(provider, exam_slug, session, limit_pages=args.pages)
            elif grep_str:
                print(f"Scraping discussions for '{provider}' matching '{grep_str}'...", file=sys.stderr)
                links = get_discussion_links(provider, grep_str, session, limit_pages=args.pages)
            else:
                print("Scraping all discussions...", file=sys.stderr)
                links = get_discussion_links(provider, grep_str, session, limit_pages=args.pages)
            print(f"Scraping {len(links)} questions...", file=sys.stderr)
            data = scrape_questions_concurrently(links, session)
            write_output(data, output_path, args.comments, args.type, provider=provider, exam_name=exam_slug)
            print(f"Saved {len(data)} questions to {display_path}")

        elif info["type"] == "exam_list":
            provider = info["provider"]
            exam_slug = info["exam_slug"]
            grep_str = args.search or ""
            if not grep_str and exam_slug:
                links = scrape_exam_via_discussions(provider, exam_slug, session, limit_pages=args.pages)
            elif grep_str:
                links = get_discussion_links(provider, grep_str, session, limit_pages=args.pages)
            else:
                print(f"Listing exams for provider '{provider}' not implemented via discussions", file=sys.stderr)
                print(f"Use: python examtopics.py -p {provider} -s <exam_code>", file=sys.stderr)
                sys.exit(1)
            print(f"Scraping {len(links)} questions...", file=sys.stderr)
            data = scrape_questions_concurrently(links, session)
            write_output(data, output_path, args.comments, args.type, provider=provider, exam_name=exam_slug or base)
            print(f"Saved {len(data)} questions to {display_path}")

        else:
            print("Could not determine URL type", file=sys.stderr)
            sys.exit(1)

    elif args.provider:
        grep_str = args.search or ""
        print(f"Scraping discussions for '{args.provider}' matching '{grep_str or 'everything'}'...", file=sys.stderr)
        links = get_discussion_links(args.provider, grep_str, session, limit_pages=args.pages)
        print(f"Scraping {len(links)} questions...", file=sys.stderr)
        data = scrape_questions_concurrently(links, session)
        write_output(data, output_path, args.comments, args.type, provider=args.provider, exam_name=base)
        print(f"Saved {len(data)} questions to {display_path}")

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
