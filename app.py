import os
import queue
import threading
import uuid
import json
from urllib.parse import urljoin

import requests
from flask import Flask, Response, jsonify, render_template, request

app = Flask(__name__)

jobs: dict[str, dict] = {}
jobs_lock = threading.Lock()
JOB_META_FILE = "job.json"


def concat_file_path(path: str) -> str:
    return path.replace("\\", "/").replace("'", "'\\''")


def shell_quote(path: str) -> str:
    return '"' + path.replace('"', '\\"') + '"'


def make_ffmpeg_cmd(file_list: str, output_folder: str, output_mp4: str = "output.mp4") -> str:
    abs_file_list = os.path.abspath(file_list)
    abs_output = os.path.abspath(os.path.join(output_folder, output_mp4))
    return (
        f"ffmpeg -y -f concat -safe 0 "
        f"-i {shell_quote(abs_file_list)} "
        f"-c:v libx264 -c:a aac "
        f"-movflags +faststart "
        f"{shell_quote(abs_output)}"
    )


def make_job() -> dict:
    return {
        "state": {
            "running": False,
            "progress": 0,
            "total": 0,
            "last_output_folder": "",
            "last_file_list": "",
            "ffmpeg_cmd": "",
        },
        "log_queue": queue.Queue(),
    }


def same_job_meta(left: dict, right: dict) -> bool:
    if left.get("mode") != right.get("mode"):
        return False
    if left.get("mode") == "m3u8":
        return left.get("m3u8_url") == right.get("m3u8_url")
    if left.get("mode") == "template":
        return (
            left.get("url_template") == right.get("url_template")
            and left.get("start") == right.get("start")
            and left.get("end") == right.get("end")
        )
    return False


def find_existing_job_folder(output_folder: str, meta: dict) -> tuple[str, str] | None:
    if not os.path.isdir(output_folder):
        return None

    for name in os.listdir(output_folder):
        folder = os.path.join(output_folder, name)
        meta_path = os.path.join(folder, JOB_META_FILE)
        if not os.path.isdir(folder) or not os.path.exists(meta_path):
            continue
        try:
            with open(meta_path, "r", encoding="utf-8") as f:
                existing_meta = json.load(f)
        except (OSError, json.JSONDecodeError):
            continue
        if same_job_meta(existing_meta, meta):
            return name, folder
    return None


def write_job_meta(output_folder: str, meta: dict):
    os.makedirs(output_folder, exist_ok=True)
    meta_path = os.path.join(output_folder, JOB_META_FILE)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)


def is_reusable_file(path: str) -> bool:
    try:
        return os.path.exists(path) and os.path.getsize(path) > 0
    except OSError:
        return False


def fetch_segments(m3u8_url: str, headers: dict, output_folder: str | None = None, depth: int = 0) -> list[str]:
    if depth > 3:
        raise ValueError("m3u8 참조가 너무 깊습니다.")

    resp = requests.get(m3u8_url, timeout=15, headers=headers)
    resp.raise_for_status()

    if output_folder:
        filename = "playlist.m3u8" if depth == 0 else f"playlist_{depth}.m3u8"
        playlist_path = os.path.join(output_folder, filename)
        with open(playlist_path, "w", encoding="utf-8") as f:
            f.write(resp.text)

    base_url = m3u8_url.rsplit("/", 1)[0] + "/"
    lines = [line.strip() for line in resp.text.splitlines()]
    urls = [
        urljoin(base_url, line.strip())
        for line in lines
        if line and not line.startswith("#")
    ]

    if any(line.startswith("#EXT-X-STREAM-INF") for line in lines):
        child_playlist = next((url for url in urls if url.lower().split("?", 1)[0].endswith(".m3u8")), None)
        if not child_playlist:
            raise ValueError("master m3u8에서 하위 playlist를 찾을 수 없습니다.")
        return fetch_segments(child_playlist, headers, output_folder, depth + 1)

    return urls


def _finalize_job(st: dict, lq: queue.Queue, downloaded_files: list, total: int, output_folder: str):
    abs_folder = os.path.abspath(output_folder)
    list_path = os.path.join(abs_folder, "file_list.txt")
    if downloaded_files:
        try:
            with open(list_path, "w", encoding="utf-8") as f:
                for abs_file in downloaded_files:
                    f.write(f"file '{concat_file_path(abs_file)}'\n")
        except OSError as e:
            lq.put(f"SYS|file_list.txt를 만들 수 없습니다: {e}")
            lq.put("JOB_ERROR")
            return
        st["last_output_folder"] = output_folder
        st["last_file_list"] = list_path
        st["ffmpeg_cmd"] = make_ffmpeg_cmd(list_path, abs_folder)
        lq.put(f"SYS|file_list.txt 생성 완료 ({len(downloaded_files)}줄): {list_path}")
        if len(downloaded_files) < total:
            lq.put(f"SYS|일부 다운로드가 실패하여 성공한 파일 {len(downloaded_files)}개만 변환 목록에 포함했습니다.")
        lq.put(f"FFMPEG_CMD|{st['ffmpeg_cmd']}")
    else:
        lq.put("SYS|성공한 다운로드가 없어 file_list.txt를 만들지 않았습니다.")
        lq.put("JOB_ERROR")


def download_m3u8_task(job_id: str, m3u8_url: str, output_folder: str, headers: dict):
    job = jobs.get(job_id)
    if not job:
        return
    st = job["state"]
    lq = job["log_queue"]

    st["progress"] = 0
    downloaded_files = []
    stopped = False

    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as e:
        st["running"] = False
        lq.put(f"SYS|저장 폴더를 만들 수 없습니다: {output_folder} — {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    lq.put("SYS|m3u8 파싱 중...")
    try:
        segments = fetch_segments(m3u8_url, headers, output_folder)
    except (requests.exceptions.RequestException, ValueError, OSError) as e:
        st["running"] = False
        lq.put(f"SYS|m3u8 다운로드 실패: {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    total = len(segments)
    if total == 0:
        st["running"] = False
        lq.put("SYS|세그먼트를 찾을 수 없습니다.")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    st["total"] = total
    lq.put(f"SYS|세그먼트 {total}개 발견")

    for i, seg_url in enumerate(segments, start=1):
        if not st["running"]:
            stopped = True
            lq.put("STOPPED")
            break

        filename = f"index{i}.ts"
        filepath = os.path.join(output_folder, filename)
        if is_reusable_file(filepath):
            downloaded_files.append(os.path.abspath(filepath))
            lq.put(f"SKIP|{i}|{total}|{filename}")
            st["progress"] = i
            continue

        try:
            resp = requests.get(seg_url, timeout=30, headers=headers)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            downloaded_files.append(os.path.abspath(filepath))
            msg = f"OK|{i}|{total}|{filename}"
        except (requests.exceptions.RequestException, OSError) as e:
            msg = f"ERR|{i}|{total}|{seg_url} — {e}"

        lq.put(msg)
        st["progress"] = i

    st["running"] = False
    if not stopped and st["progress"] == total:
        _finalize_job(st, lq, downloaded_files, total, output_folder)

    lq.put("DONE")


def download_task(job_id: str, url_template: str, start: int, end: int, output_folder: str, headers: dict):
    job = jobs.get(job_id)
    if not job:
        return
    st = job["state"]
    lq = job["log_queue"]

    total = end - start + 1
    st["total"] = total
    st["progress"] = 0
    st["last_output_folder"] = ""
    st["last_file_list"] = ""
    st["ffmpeg_cmd"] = ""
    downloaded_files = []
    stopped = False

    try:
        os.makedirs(output_folder, exist_ok=True)
    except OSError as e:
        st["running"] = False
        lq.put(f"SYS|저장 폴더를 만들 수 없습니다: {output_folder} — {e}")
        lq.put("JOB_ERROR")
        lq.put("DONE")
        return

    for i in range(start, end + 1):
        if not st["running"]:
            stopped = True
            lq.put("STOPPED")
            break

        current = i - start + 1
        try:
            url = url_template.format(i)
            filename = f"index{i}.ts"
        except (IndexError, KeyError, ValueError) as e:
            lq.put(f"ERR|{current}|{total}|템플릿 형식 오류: {e}")
            st["progress"] = current
            continue

        filepath = os.path.join(output_folder, filename)
        if is_reusable_file(filepath):
            downloaded_files.append(os.path.abspath(filepath))
            lq.put(f"SKIP|{current}|{total}|{filename}")
            st["progress"] = current
            continue

        try:
            resp = requests.get(url, timeout=30, headers=headers)
            resp.raise_for_status()
            with open(filepath, "wb") as f:
                f.write(resp.content)
            downloaded_files.append(os.path.abspath(filepath))
            msg = f"OK|{current}|{total}|{filename}"
        except (requests.exceptions.RequestException, OSError) as e:
            msg = f"ERR|{current}|{total}|{url} — {e}"

        lq.put(msg)
        st["progress"] = current

    st["running"] = False
    if not stopped and st["progress"] == total:
        _finalize_job(st, lq, downloaded_files, total, output_folder)

    lq.put("DONE")


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/start", methods=["POST"])
def start():
    data = request.get_json(silent=True) or {}
    m3u8_url    = (data.get("m3u8_url")    or "").strip()
    url_template = (data.get("url_template") or "").strip()

    output_folder = (data.get("output_folder") or "./downloads").strip()
    headers = {
        "User-Agent": (data.get("user_agent") or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36").strip(),
    }
    referer = (data.get("referer") or "").strip()
    if referer:
        headers["Referer"] = referer

    if m3u8_url:
        task_target = download_m3u8_task
        task_args = (m3u8_url,)
        job_meta = {
            "mode": "m3u8",
            "m3u8_url": m3u8_url,
        }
        total = 0
    elif url_template:
        if "{" not in url_template:
            return jsonify({"error": "URL 템플릿에 {} 또는 {:06d} 자리표시자가 필요합니다."}), 400
        try:
            start_idx = int(data.get("start", 1))
            end_idx   = int(data.get("end", 10))
        except (TypeError, ValueError):
            return jsonify({"error": "시작 번호와 끝 번호는 숫자여야 합니다."}), 400
        if start_idx > end_idx:
            return jsonify({"error": "시작 번호는 끝 번호보다 클 수 없습니다."}), 400
        task_target = download_task
        task_args = (url_template, start_idx, end_idx)
        job_meta = {
            "mode": "template",
            "url_template": url_template,
            "start": start_idx,
            "end": end_idx,
        }
        total = end_idx - start_idx + 1
    else:
        return jsonify({"error": "m3u8 URL 또는 URL 템플릿을 입력하세요."}), 400

    with jobs_lock:
        existing_job = find_existing_job_folder(output_folder, job_meta)
        if existing_job:
            job_id, job_output_folder = existing_job
            reused_job = True
            if job_id in jobs and jobs[job_id]["state"]["running"]:
                return jsonify({"error": f"이미 실행 중인 작업입니다: {job_id}"}), 400
        else:
            job_id = uuid.uuid4().hex[:8]
            while job_id in jobs or os.path.exists(os.path.join(output_folder, job_id)):
                job_id = uuid.uuid4().hex[:8]
            job_output_folder = os.path.join(output_folder, job_id)
            reused_job = False
        jobs[job_id] = make_job()

    try:
        write_job_meta(job_output_folder, job_meta)
    except OSError as e:
        with jobs_lock:
            jobs.pop(job_id, None)
        return jsonify({"error": f"작업 메타데이터를 저장할 수 없습니다: {e}"}), 400

    st = jobs[job_id]["state"]
    st["running"] = True
    st["progress"] = 0
    st["total"] = total
    st["last_output_folder"] = job_output_folder
    st["last_file_list"] = ""
    st["ffmpeg_cmd"] = ""
    if reused_job:
        jobs[job_id]["log_queue"].put(f"SYS|기존 작업 폴더를 재사용합니다: {job_output_folder}")

    if task_target is download_m3u8_task:
        args = (job_id, task_args[0], job_output_folder, headers)
    else:
        args = (job_id, task_args[0], task_args[1], task_args[2], job_output_folder, headers)

    t = threading.Thread(target=task_target, args=args, daemon=True)
    t.start()
    return jsonify({"status": "started", "job_id": job_id})


@app.route("/stop/<job_id>", methods=["POST"])
def stop(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    job["state"]["running"] = False
    return jsonify({"status": "stopped"})


@app.route("/status/<job_id>")
def status(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return jsonify({"error": "작업을 찾을 수 없습니다."}), 404
    st = job["state"]
    return jsonify({
        "running": st["running"],
        "progress": st["progress"],
        "total": st["total"],
        "file_list": st["last_file_list"],
        "ffmpeg_cmd": st["ffmpeg_cmd"],
    })


@app.route("/stream/<job_id>")
def stream(job_id: str):
    job = jobs.get(job_id)
    if not job:
        return Response("job not found", status=404)

    lq = job["log_queue"]
    st = job["state"]

    def generate():
        while True:
            try:
                msg = lq.get(timeout=1.5)
                yield f"data: {msg}\n\n"
                if msg in ("DONE", "STOPPED"):
                    break
            except queue.Empty:
                if not st["running"]:
                    break
                yield "data: PING\n\n"

    return Response(generate(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

if __name__ == "__main__":
    app.run(debug=True, port=5000, threaded=True)
