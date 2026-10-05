from __future__ import annotations

import argparse
import asyncio
import csv
import json
import random
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

from playwright.async_api import async_playwright, Browser

SITE_ORIGIN = "https://kyou.net.cn"
SONGLIST_PATH = "/api/songs/songlist"
TAG_TREE_PATH = "/api/tags/tree"
TOP_BATCH_PATH = "/api/tags/top-batch"
STANDARD_DIFFS = ["ez", "hd", "in", "at"]


def write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2), encoding="utf-8")


def write_csv(path: Path, rows: list[dict[str, Any]], fields: list[str] | None = None) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if fields is None:
        fields = list(rows[0].keys()) if rows else []
    if not fields:
        path.write_text("", encoding="utf-8-sig")
        return
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)


def chunks(items: list[str], size: int):
    for i in range(0, len(items), size):
        yield i // size, items[i:i + size]


def nonzero_number(v: Any) -> bool:
    if v is None or v == "":
        return False
    try:
        return float(v) != 0
    except (TypeError, ValueError):
        return False


def aliases_from(value: Any) -> list[str]:
    if isinstance(value, list):
        return list(dict.fromkeys(str(x).strip() for x in value if str(x).strip()))
    if isinstance(value, str) and value.strip():
        # The live site normally uses an array. This is only a compatibility fallback.
        return [x.strip() for x in value.replace("；", ";").split(";") if x.strip()]
    return []


def root_catalog(tag_tree: list[dict[str, Any]]) -> tuple[dict[int, str], dict[int, dict[str, Any]]]:
    roots: dict[int, str] = {}
    all_tags: dict[int, dict[str, Any]] = {}
    for root in tag_tree:
        try:
            rid = int(root.get("id"))
        except Exception:
            continue
        roots[rid] = str(root.get("name") or rid)
        all_tags[rid] = root
        for child in root.get("children") or []:
            try:
                cid = int(child.get("id"))
            except Exception:
                continue
            all_tags[cid] = child
    return roots, all_tags


def flatten_catalog(tag_tree: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen: set[tuple[int, str]] = set()
    for root in tag_tree:
        rid = root.get("id")
        rows.append({
            "tag_id": rid,
            "tag": root.get("name", ""),
            "tag_type": "primary",
            "parent_ids": "",
            "description": root.get("description") or "",
        })
        for child in root.get("children") or []:
            cid = child.get("id")
            key = (cid, str(child.get("name") or ""))
            if key in seen:
                continue
            seen.add(key)
            pids = child.get("parentIds") or [rid]
            rows.append({
                "tag_id": cid,
                "tag": child.get("name", ""),
                "tag_type": "secondary",
                "parent_ids": "|".join(str(x) for x in pids),
                "description": child.get("description") or "",
            })
    return rows


class IsolatedBrowserClient:
    """Make API calls from a fresh browser context.

    The site currently lets a clean Chromium context's first top-level request
    through Cloudflare more reliably than requests made after /songs has loaded.
    GET therefore uses direct document navigation. POST uses a locally-fulfilled
    same-origin bootstrap page, so the POST is the first real network request.
    """

    def __init__(self, browser: Browser, out: Path, retries: int, timeout_ms: int, pause_ms: int):
        self.browser = browser
        self.out = out
        self.retries = retries
        self.timeout_ms = timeout_ms
        self.pause_ms = pause_ms
        self.attempts: list[dict[str, Any]] = []

    async def _pause(self, attempt: int = 0) -> None:
        ms = self.pause_ms + attempt * 700 + random.randint(0, 350)
        await asyncio.sleep(ms / 1000)

    def _record(self, **item: Any) -> None:
        self.attempts.append(item)
        write_json(self.out / "api_attempts.json", self.attempts)

    async def get_json(self, path: str, purpose: str, retries: int | None = None) -> Any | None:
        tries = retries if retries is not None else self.retries
        url = path if path.startswith("http") else SITE_ORIGIN + path
        for attempt in range(tries):
            context = await self.browser.new_context(locale="zh-CN")
            page = await context.new_page()
            started = time.time()
            status = None
            ct = ""
            body = ""
            error = ""
            try:
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                status = response.status if response else None
                ct = (response.headers.get("content-type") or "") if response else ""
                body = await page.locator("body").inner_text(timeout=5000)
                parsed = json.loads(body) if body.strip() else None
                ok = status is not None and 200 <= status < 300
                self._record(
                    purpose=purpose, method="GET", strategy="fresh_document",
                    url=url, attempt=attempt + 1, status=status, content_type=ct,
                    ok=ok, elapsed_ms=int((time.time() - started) * 1000),
                    body_prefix=body[:500],
                )
                if ok:
                    return parsed
            except Exception as e:
                error = repr(e)
                self._record(
                    purpose=purpose, method="GET", strategy="fresh_document",
                    url=url, attempt=attempt + 1, status=status, content_type=ct,
                    ok=False, elapsed_ms=int((time.time() - started) * 1000),
                    body_prefix=body[:500], error=error,
                )
            finally:
                await context.close()
            await self._pause(attempt)
        return None

    async def post_json(self, path: str, payload: dict[str, Any], purpose: str, retries: int | None = None) -> Any | None:
        tries = retries if retries is not None else self.retries
        url = path if path.startswith("http") else SITE_ORIGIN + path
        bootstrap = SITE_ORIGIN + "/__kyou_crawler_bootstrap__"

        for attempt in range(tries):
            # Strategy A: turn a top-level navigation into POST via request
            # interception. This keeps Sec-Fetch-Dest=document, which matters
            # because Cloudflare currently treats document and fetch requests
            # differently on this site.
            context = await self.browser.new_context(locale="zh-CN")
            page = await context.new_page()
            started = time.time()
            status = None
            body = ""
            ct = ""

            async def rewrite_as_post(route):
                headers = dict(route.request.headers)
                headers.pop("content-length", None)
                headers["content-type"] = "application/json"
                headers["accept"] = "application/json,text/plain,*/*"
                await route.continue_(
                    method="POST",
                    headers=headers,
                    post_data=json.dumps(payload, ensure_ascii=False),
                )

            try:
                await page.route(url, rewrite_as_post)
                response = await page.goto(url, wait_until="domcontentloaded", timeout=self.timeout_ms)
                status = response.status if response else None
                ct = (response.headers.get("content-type") or "") if response else ""
                body = await page.locator("body").inner_text(timeout=5000)
                ok = status is not None and 200 <= status < 300
                self._record(
                    purpose=purpose, method="POST", strategy="fresh_document_post_override",
                    url=url, attempt=attempt + 1, status=status, content_type=ct,
                    ok=ok, elapsed_ms=int((time.time() - started) * 1000),
                    request_summary={
                        "difficulty": payload.get("difficulty"),
                        "song_count": len(payload.get("songIds") or []),
                        "limitPerSong": payload.get("limitPerSong"),
                    },
                    body_prefix=body[:500],
                )
                if ok:
                    return json.loads(body) if body.strip() else None
            except Exception as e:
                self._record(
                    purpose=purpose, method="POST", strategy="fresh_document_post_override",
                    url=url, attempt=attempt + 1, status=status, content_type=ct,
                    ok=False, elapsed_ms=int((time.time() - started) * 1000),
                    request_summary={
                        "difficulty": payload.get("difficulty"),
                        "song_count": len(payload.get("songIds") or []),
                    },
                    error=repr(e), body_prefix=body[:500],
                )
            finally:
                await context.close()

            await self._pause(attempt)

            # Strategy B: same-origin bootstrap fulfilled locally, making this
            # POST fetch the first real network request in another clean context.
            context = await self.browser.new_context(locale="zh-CN")
            page = await context.new_page()

            async def local_bootstrap(route):
                await route.fulfill(
                    status=200,
                    content_type="text/html; charset=utf-8",
                    body="<!doctype html><meta charset=utf-8><title>bootstrap</title>",
                )

            await page.route(bootstrap, local_bootstrap)
            started = time.time()
            result: dict[str, Any] = {}
            try:
                await page.goto(bootstrap, wait_until="domcontentloaded", timeout=10000)
                result = await page.evaluate(
                    """async ({url, payload}) => {
                      try {
                        const r = await fetch(url, {
                          method: 'POST',
                          headers: {'Content-Type':'application/json','Accept':'application/json'},
                          body: JSON.stringify(payload),
                          credentials: 'include',
                          cache: 'no-store'
                        });
                        return {
                          status: r.status,
                          contentType: r.headers.get('content-type') || '',
                          text: await r.text()
                        };
                      } catch (e) {
                        return {status: 0, contentType: '', text: '', error: String(e)};
                      }
                    }""",
                    {"url": url, "payload": payload},
                )
                status = int(result.get("status") or 0)
                body = result.get("text") or ""
                ct = result.get("contentType") or ""
                ok = 200 <= status < 300
                self._record(
                    purpose=purpose, method="POST", strategy="fresh_context_first_fetch",
                    url=url, attempt=attempt + 1, status=status, content_type=ct,
                    ok=ok, elapsed_ms=int((time.time() - started) * 1000),
                    request_summary={
                        "difficulty": payload.get("difficulty"),
                        "song_count": len(payload.get("songIds") or []),
                        "limitPerSong": payload.get("limitPerSong"),
                    },
                    body_prefix=body[:500], error=result.get("error") or "",
                )
                if ok:
                    return json.loads(body) if body.strip() else None
            except Exception as e:
                self._record(
                    purpose=purpose, method="POST", strategy="fresh_context_first_fetch",
                    url=url, attempt=attempt + 1, status=result.get("status"),
                    ok=False, elapsed_ms=int((time.time() - started) * 1000),
                    request_summary={
                        "difficulty": payload.get("difficulty"),
                        "song_count": len(payload.get("songIds") or []),
                    }, error=repr(e), body_prefix=(result.get("text") or "")[:500],
                )
            finally:
                await context.close()

            await self._pause(attempt)
        return None



class Dataset:
    def __init__(self, tag_tree: list[dict[str, Any]], songlist_payload: Any):
        self.tag_tree = tag_tree
        self.root_names, self.all_tags = root_catalog(tag_tree)
        self.songlist_payload = songlist_payload
        self.song_items = songlist_payload if isinstance(songlist_payload, list) else (songlist_payload or {}).get("data") or []
        self.songs: list[dict[str, Any]] = []
        self.aliases: list[dict[str, Any]] = []
        self.charts: list[dict[str, Any]] = []
        self.chart_map: dict[tuple[str, str], dict[str, Any]] = {}
        self.votes: list[dict[str, Any]] = []
        self._vote_keys: set[tuple[str, str, str, str]] = set()
        self._parse_songlist()

    def _parse_songlist(self) -> None:
        seen_songs: set[str] = set()
        for song in self.song_items:
            if not isinstance(song, dict):
                continue
            sid = str(song.get("id") or song.get("songId") or "").strip()
            if not sid:
                continue
            name = str(song.get("标题") or song.get("title") or song.get("name") or "").strip()
            aliases = aliases_from(song.get("别称") if "别称" in song else song.get("aliases"))
            if sid not in seen_songs:
                self.songs.append({
                    "song_id": sid,
                    "name": name,
                    "pack": str(song.get("曲包") or song.get("pack") or ""),
                })
                seen_songs.add(sid)
            for alias in aliases:
                self.aliases.append({"song_id": sid, "song_name": name, "alias": alias})

            diff_obj = song.get("难度") if isinstance(song.get("难度"), dict) else song.get("difficulty")
            if not isinstance(diff_obj, dict):
                continue
            keys = [d for d in STANDARD_DIFFS if d in diff_obj]
            keys += [str(d) for d in diff_obj.keys() if str(d) not in keys]
            for diff in keys:
                constant = diff_obj.get(diff)
                if not nonzero_number(constant):
                    continue
                chart_id = f"{sid}::{str(diff).lower()}"
                row = {
                    "chart_id": chart_id,
                    "song_id": sid,
                    "song_name": name,
                    "difficulty": str(diff).lower(),
                    "constant": constant,
                    "main_label": "",
                    "main_label_question": False,
                    "main_top_votes": 0,
                    "main_second_votes": 0,
                    "tag_source": "",
                }
                self.charts.append(row)
                self.chart_map[(sid, str(diff).lower())] = row

    def song_ids_for_diff(self, diff: str) -> list[str]:
        return [r["song_id"] for r in self.charts if r["difficulty"] == diff]

    def add_vote(self, sid: str, diff: str, tag_type: str, tag_id: Any, tag: str, votes: Any,
                 parent_ids: list[Any] | None, source: str) -> None:
        chart = self.chart_map.get((str(sid), str(diff).lower()))
        if not chart:
            return
        key = (chart["chart_id"], tag_type, str(tag_id), source)
        if key in self._vote_keys:
            return
        self._vote_keys.add(key)
        self.votes.append({
            "chart_id": chart["chart_id"],
            "song_id": chart["song_id"],
            "song_name": chart["song_name"],
            "difficulty": chart["difficulty"],
            "tag_type": tag_type,
            "tag_id": tag_id,
            "tag": tag,
            "votes": int(votes or 0),
            "parent_ids": "|".join(str(x) for x in (parent_ids or [])),
            "source": source,
        })

    def apply_chart_votes(self, sid: str, diff: str, secondary: list[dict[str, Any]],
                          parent_totals: dict[Any, Any], source: str) -> None:
        sid = str(sid)
        diff = str(diff).lower()
        chart = self.chart_map.get((sid, diff))
        if not chart:
            return

        # Main tags are the five real root categories. “综合” is derived by the
        # front end from the leading two parent totals, not a separate root tag.
        normalized_totals: dict[int, int] = {}
        for rid in self.root_names:
            raw = parent_totals.get(str(rid), parent_totals.get(rid, 0)) if isinstance(parent_totals, dict) else 0
            try:
                normalized_totals[rid] = int(raw or 0)
            except Exception:
                normalized_totals[rid] = 0
            self.add_vote(sid, diff, "primary", rid, self.root_names[rid], normalized_totals[rid], [], source)

        for item in secondary or []:
            if not isinstance(item, dict):
                continue
            tid = item.get("tagId", item.get("id"))
            tag = str(item.get("tagName") or item.get("name") or self.all_tags.get(int(tid), {}).get("name") if tid is not None else "")
            parents = item.get("parentIds") or (self.all_tags.get(int(tid), {}).get("parentIds") if tid is not None and str(tid).isdigit() else []) or []
            self.add_vote(sid, diff, "secondary", tid, tag, item.get("voteCount", 0), parents, source)

        ranked = sorted(normalized_totals.items(), key=lambda kv: (-kv[1], list(self.root_names).index(kv[0])))
        if ranked:
            top_id, top = ranked[0]
            second = ranked[1][1] if len(ranked) > 1 else 0
            total = sum(normalized_totals.values())
            comprehensive = bool(total > 0 and (top / total - second / total) <= 0.1)
            chart["main_label"] = "综合" if comprehensive else (self.root_names.get(top_id, "") if top > 0 else "")
            chart["main_label_question"] = bool(top > 0 and top < 20)
            chart["main_top_votes"] = top
            chart["main_second_votes"] = second
            chart["tag_source"] = source

    def apply_batch(self, diff: str, payload: Any) -> int:
        if not isinstance(payload, dict):
            return 0
        count = 0
        for sid, entry in payload.items():
            if not isinstance(entry, dict):
                continue
            self.apply_chart_votes(
                str(sid), diff,
                entry.get("topTags") or [],
                entry.get("parentTagTotals") or {},
                "top-batch",
            )
            count += 1
        return count

    def apply_tree(self, sid: str, diff: str, tree: Any) -> None:
        if not isinstance(tree, list):
            return
        secondary_by_id: dict[int, dict[str, Any]] = {}
        totals = {rid: 0 for rid in self.root_names}
        for root in tree:
            if not isinstance(root, dict):
                continue
            for child in root.get("children") or []:
                if not isinstance(child, dict):
                    continue
                try:
                    tid = int(child.get("id"))
                except Exception:
                    continue
                current = secondary_by_id.get(tid)
                if current is None or int(child.get("voteCount") or 0) > int(current.get("voteCount") or 0):
                    secondary_by_id[tid] = child
        for child in secondary_by_id.values():
            votes = int(child.get("voteCount") or 0)
            pids = child.get("parentIds") or []
            for pid in pids:
                try:
                    pid_i = int(pid)
                except Exception:
                    continue
                if pid_i in totals:
                    totals[pid_i] += votes
        self.apply_chart_votes(sid, diff, list(secondary_by_id.values()), totals, "tree-per-chart")

    def nested(self) -> list[dict[str, Any]]:
        aliases: dict[str, list[str]] = {}
        for a in self.aliases:
            aliases.setdefault(a["song_id"], []).append(a["alias"])
        vote_map: dict[str, list[dict[str, Any]]] = {}
        for v in self.votes:
            vote_map.setdefault(v["chart_id"], []).append(v)
        charts_by_song: dict[str, list[dict[str, Any]]] = {}
        for c in self.charts:
            item = dict(c)
            vv = vote_map.get(c["chart_id"], [])
            item["primary_votes"] = [x for x in vv if x["tag_type"] == "primary"]
            item["secondary_votes"] = [x for x in vv if x["tag_type"] == "secondary"]
            charts_by_song.setdefault(c["song_id"], []).append(item)
        return [
            {**s, "aliases": aliases.get(s["song_id"], []), "charts": charts_by_song.get(s["song_id"], [])}
            for s in self.songs
        ]


async def fetch_tree_fallback(client: IsolatedBrowserClient, dataset: Dataset, out: Path,
                              concurrency: int, retries: int) -> tuple[int, int]:
    sem = asyncio.Semaphore(max(1, concurrency))
    raw_dir = out / "raw" / "chart_trees"
    raw_dir.mkdir(parents=True, exist_ok=True)
    ok = 0
    failed = 0
    lock = asyncio.Lock()

    async def one(chart: dict[str, Any]) -> None:
        nonlocal ok, failed
        sid = chart["song_id"]
        diff = chart["difficulty"]
        path = TAG_TREE_PATH + "?" + urlencode({"songId": sid, "difficulty": diff})
        async with sem:
            tree = await client.get_json(path, f"chart-tree:{sid}:{diff}", retries=retries)
        async with lock:
            if isinstance(tree, list):
                ok += 1
                dataset.apply_tree(sid, diff, tree)
                write_json(raw_dir / f"{sid}__{diff}.json", tree)
            else:
                failed += 1
            done = ok + failed
            if done % 50 == 0 or done == len(dataset.charts):
                print(f"  单谱 GET 进度 {done}/{len(dataset.charts)}，成功 {ok}，失败 {failed}")

    await asyncio.gather(*(one(c) for c in dataset.charts))
    return ok, failed


async def run(args: argparse.Namespace) -> int:
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    (out / "raw").mkdir(exist_ok=True)
    started = time.time()

    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=not args.headful)
        client = IsolatedBrowserClient(browser, out, args.retries, args.timeout_ms, args.pause_ms)

        print("[1/6] 新上下文直取标签目录")
        tag_tree = await client.get_json(TAG_TREE_PATH, "tag-catalog")
        if not isinstance(tag_tree, list) or not tag_tree:
            write_json(out / "manifest.json", {"ok": False, "reason": "tag_catalog_failed", "started_unix": started, "finished_unix": time.time()})
            await browser.close()
            print("错误：连标签目录都无法通过隔离上下文获取。", file=sys.stderr)
            return 3
        write_json(out / "raw" / "tag_catalog.json", tag_tree)
        catalog_rows = flatten_catalog(tag_tree)
        write_json(out / "tag_catalog.json", catalog_rows)
        write_csv(out / "tag_catalog.csv", catalog_rows, ["tag_id", "tag", "tag_type", "parent_ids", "description"])
        print(f"  标签目录：{len(catalog_rows)} 条")

        print("[2/6] 新上下文直取完整歌曲列表")
        songlist = await client.get_json(SONGLIST_PATH, "songlist")
        if not isinstance(songlist, (dict, list)):
            write_json(out / "manifest.json", {"ok": False, "reason": "songlist_failed", "started_unix": started, "finished_unix": time.time()})
            await browser.close()
            print("错误：/api/songs/songlist 仍被 Cloudflare 拦截。", file=sys.stderr)
            return 5
        write_json(out / "raw" / "songlist.json", songlist)
        dataset = Dataset(tag_tree, songlist)
        print(f"  歌曲 {len(dataset.songs)}，谱面 {len(dataset.charts)}，别名 {len(dataset.aliases)}")
        if not dataset.songs or not dataset.charts:
            await browser.close()
            print("错误：songlist 返回成功，但字段结构未解析出歌曲/谱面。", file=sys.stderr)
            return 4

        print("[3/6] 尝试批量 POST 获取主/次标签票数")
        diffs = []
        for d in STANDARD_DIFFS + sorted({c["difficulty"] for c in dataset.charts}):
            if d not in diffs and dataset.song_ids_for_diff(d):
                diffs.append(d)

        batch_ok = True
        batch_calls = 0
        batch_song_results = 0
        raw_batch_dir = out / "raw" / "top_batch"
        raw_batch_dir.mkdir(parents=True, exist_ok=True)

        for diff in diffs:
            ids = dataset.song_ids_for_diff(diff)
            for chunk_no, batch_ids in chunks(ids, args.batch_size):
                payload = {"songIds": batch_ids, "difficulty": diff, "limitPerSong": args.limit_per_song}
                result = await client.post_json(TOP_BATCH_PATH, payload, f"top-batch:{diff}:{chunk_no}")
                batch_calls += 1
                if not isinstance(result, dict):
                    batch_ok = False
                    print(f"  批量 POST 被拦：{diff} 第 {chunk_no + 1} 批；切换单谱 GET 方案")
                    break
                write_json(raw_batch_dir / f"{diff}_{chunk_no:03d}.json", result)
                batch_song_results += dataset.apply_batch(diff, result)
                # Some batch APIs omit songs that currently have zero votes.
                # An omitted requested song is therefore recorded as a valid
                # all-zero chart instead of being mistaken for a crawl failure.
                returned_ids = {str(x) for x in result.keys()}
                for missing_sid in batch_ids:
                    if str(missing_sid) not in returned_ids:
                        dataset.apply_chart_votes(str(missing_sid), diff, [], {}, "top-batch")
                print(f"  {diff.upper()} 第 {chunk_no + 1} 批：{len(batch_ids)} 首，返回 {len(result)} 首")
                await asyncio.sleep(args.pause_ms / 1000)
            if not batch_ok:
                break

        fallback_ok = fallback_failed = 0
        strategy = "top-batch" if batch_ok else "batch-failed"
        if not batch_ok:
            # Discard incomplete vote rows from partially completed batches so
            # every chart comes from one consistent strategy.
            dataset.votes.clear()
            dataset._vote_keys.clear()
            for c in dataset.charts:
                c.update({"main_label": "", "main_label_question": False, "main_top_votes": 0,
                          "main_second_votes": 0, "tag_source": ""})
            if args.tree_fallback:
                strategy = "tree-per-chart"
                print("[4/6] 降级：每张谱面用新上下文 GET /api/tags/tree?songId&difficulty")
                fallback_ok, fallback_failed = await fetch_tree_fallback(
                    client, dataset, out, args.tree_concurrency, args.tree_retries
                )
            else:
                # Scheduled jobs do not automatically fire thousands of requests.
                # Probe a few charts so the artifact still tells us whether the
                # per-chart GET fallback is viable, then fail cleanly.
                print("[4/6] 批量 POST 失败；仅探测 3 张谱面（定时任务默认不进行数千次逐谱请求）")
                probe_charts = dataset.charts[:3]
                for chart in probe_charts:
                    sid, diff = chart["song_id"], chart["difficulty"]
                    path = TAG_TREE_PATH + "?" + urlencode({"songId": sid, "difficulty": diff})
                    tree = await client.get_json(path, f"fallback-probe:{sid}:{diff}", retries=1)
                    if isinstance(tree, list):
                        fallback_ok += 1
                        dataset.apply_tree(sid, diff, tree)
                    else:
                        fallback_failed += 1
        else:
            print("[4/6] 批量接口成功，不需要逐谱请求")

        print("[5/6] 输出标准表")
        song_fields = ["song_id", "name", "pack"]
        alias_fields = ["song_id", "song_name", "alias"]
        chart_fields = ["chart_id", "song_id", "song_name", "difficulty", "constant", "main_label",
                        "main_label_question", "main_top_votes", "main_second_votes", "tag_source"]
        vote_fields = ["chart_id", "song_id", "song_name", "difficulty", "tag_type", "tag_id", "tag",
                       "votes", "parent_ids", "source"]
        write_json(out / "songs.json", dataset.songs)
        write_json(out / "aliases.json", dataset.aliases)
        write_json(out / "charts.json", dataset.charts)
        write_json(out / "tag_votes.json", dataset.votes)
        write_json(out / "data.json", dataset.nested())
        write_csv(out / "songs.csv", dataset.songs, song_fields)
        write_csv(out / "aliases.csv", dataset.aliases, alias_fields)
        write_csv(out / "charts.csv", dataset.charts, chart_fields)
        write_csv(out / "tag_votes.csv", dataset.votes, vote_fields)

        charts_with_tags = sum(1 for c in dataset.charts if c.get("tag_source"))
        primary_rows = sum(1 for v in dataset.votes if v["tag_type"] == "primary")
        secondary_rows = sum(1 for v in dataset.votes if v["tag_type"] == "secondary")
        manifest = {
            "ok": bool(dataset.songs and dataset.charts and charts_with_tags),
            "source": SITE_ORIGIN,
            "started_unix": started,
            "finished_unix": time.time(),
            "last_update": songlist.get("lastUpdate", "") if isinstance(songlist, dict) else "",
            "strategy": strategy,
            "songs_rows": len(dataset.songs),
            "aliases_rows": len(dataset.aliases),
            "charts_rows": len(dataset.charts),
            "charts_with_tags": charts_with_tags,
            "tag_vote_rows": len(dataset.votes),
            "primary_vote_rows": primary_rows,
            "secondary_vote_rows": secondary_rows,
            "batch_calls": batch_calls,
            "batch_song_results": batch_song_results,
            "fallback_ok": fallback_ok,
            "fallback_failed": fallback_failed,
            "api_attempts": len(client.attempts),
            "batch_limit_per_song": args.limit_per_song,
        }
        write_json(out / "manifest.json", manifest)
        await browser.close()

    print("[6/6] 完整性检查")
    print(json.dumps(manifest, ensure_ascii=False, indent=2))
    if not manifest["songs_rows"] or not manifest["charts_rows"]:
        return 4
    if strategy == "batch-failed":
        print("错误：批量标签 POST 仍被拦截；只做了少量逐谱 GET 探测，未生成可发布的完整快照。", file=sys.stderr)
        return 8
    if manifest["charts_with_tags"] == 0:
        print("错误：歌曲和谱面抓到了，但标签投票仍全部失败。", file=sys.stderr)
        return 6
    # Partial per-chart fallback is useful as a diagnostic but not safe to
    # publish as a supposedly complete latest snapshot.
    if strategy == "tree-per-chart" and fallback_failed:
        print(f"错误：逐谱抓取有 {fallback_failed} 张失败，不应覆盖 latest。", file=sys.stderr)
        return 7
    return 0


def parse_args() -> argparse.Namespace:
    ap = argparse.ArgumentParser(description="直接调用 kyou.net.cn 真实 API，抓歌曲、别名、谱面和标签票数")
    ap.add_argument("--out", default="output")
    ap.add_argument("--headful", action="store_true", help="GitHub Actions 下配合 xvfb-run")
    ap.add_argument("--timeout-ms", type=int, default=30000)
    ap.add_argument("--retries", type=int, default=3)
    ap.add_argument("--pause-ms", type=int, default=350, help="API 调用间基础停顿，避免给站点造成突发压力")
    ap.add_argument("--batch-size", type=int, default=200, help="前端本身使用 200 首一批")
    ap.add_argument("--limit-per-song", type=int, default=100, help="批量接口返回每首歌最多多少个次标签")
    ap.add_argument("--tree-fallback", action="store_true", help="批量 POST 失败时允许逐张谱面 GET；会产生较多请求，定时任务默认关闭")
    ap.add_argument("--tree-concurrency", type=int, default=3, help="逐谱 GET 的并发数")
    ap.add_argument("--tree-retries", type=int, default=2)
    return ap.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
