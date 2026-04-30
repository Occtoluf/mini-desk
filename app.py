import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
import webbrowser
from datetime import datetime
from http import HTTPStatus
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler
from pathlib import Path
from typing import Optional
from urllib.parse import parse_qs, unquote, urlparse


BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
DATA_DIR = BASE_DIR / "data"
BACKUP_DIR = BASE_DIR / "backups"
DB_PATH = DATA_DIR / "tasks.sqlite"

STATUSES = [
    {"key": "current", "label": "Сейчас"},
    {"key": "next", "label": "Следующие"},
    {"key": "waiting", "label": "Ожидают"},
    {"key": "done", "label": "Готово"},
    {"key": "archive", "label": "Архив"},
]

WRITE_LOCK = threading.Lock()


def now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def db_connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def init_db() -> None:
    DATA_DIR.mkdir(exist_ok=True)
    BACKUP_DIR.mkdir(exist_ok=True)

    with db_connect() as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS projects (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                description TEXT NOT NULL DEFAULT '',
                sort_order INTEGER NOT NULL DEFAULT 0,
                last_task_id INTEGER,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                project_id INTEGER NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'current',
                summary TEXT NOT NULL DEFAULT '',
                next_step TEXT NOT NULL DEFAULT '',
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                archived_at TEXT,
                FOREIGN KEY (project_id) REFERENCES projects(id) ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS link_types (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL UNIQUE,
                color TEXT NOT NULL DEFAULT '#6b7280',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS links (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                task_id INTEGER NOT NULL,
                label TEXT NOT NULL,
                target TEXT NOT NULL,
                type_id INTEGER,
                notes TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                FOREIGN KEY (task_id) REFERENCES tasks(id) ON DELETE CASCADE,
                FOREIGN KEY (type_id) REFERENCES link_types(id) ON DELETE SET NULL
            );

            CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            """
        )

        type_count = conn.execute("SELECT COUNT(*) FROM link_types").fetchone()[0]
        if type_count == 0:
            defaults = [
                ("Код", "#2f7f73"),
                ("Документы", "#7c5b2a"),
                ("Схемы", "#7b4d8b"),
                ("Тикет", "#9a493f"),
                ("Прочее", "#59636e"),
            ]
            stamp = now()
            conn.executemany(
                "INSERT INTO link_types (name, color, created_at, updated_at) VALUES (?, ?, ?, ?)",
                [(name, color, stamp, stamp) for name, color in defaults],
            )

        project_count = conn.execute("SELECT COUNT(*) FROM projects").fetchone()[0]
        if project_count == 0:
            stamp = now()
            conn.execute(
                """
                INSERT INTO projects (name, description, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("Рабочие задачи", "Личный журнал задач и ссылок", 1, stamp, stamp),
            )

        conn.commit()

    daily_backup()


def daily_backup() -> None:
    if not DB_PATH.exists():
        return
    today = datetime.now().strftime("%Y-%m-%d")
    backup_path = BACKUP_DIR / f"tasks-{today}.sqlite"
    if not backup_path.exists():
        shutil.copy2(DB_PATH, backup_path)


def read_json(handler: BaseHTTPRequestHandler) -> dict:
    length = int(handler.headers.get("Content-Length", "0"))
    if length <= 0:
        return {}
    raw = handler.rfile.read(length).decode("utf-8")
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise ValueError("Некорректный JSON") from exc
    if not isinstance(data, dict):
        raise ValueError("Ожидался JSON-объект")
    return data


def json_response(handler: BaseHTTPRequestHandler, payload, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.send_header("Cache-Control", "no-store")
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler: BaseHTTPRequestHandler, status: int, message: str) -> None:
    json_response(handler, {"error": message}, status)


def require_text(data: dict, key: str, label: str) -> str:
    value = str(data.get(key, "")).strip()
    if not value:
        raise ValueError(f"Поле «{label}» обязательно")
    return value


def optional_text(data: dict, key: str) -> str:
    return str(data.get(key, "") or "").strip()


def int_or_none(value) -> Optional[int]:
    if value is None or value == "":
        return None
    return int(value)


def derive_label(target: str) -> str:
    cleaned = target.strip().rstrip("\\/")
    if not cleaned:
        return "Ссылка"
    cleaned = cleaned.replace("\\", "/")
    return cleaned.split("/")[-1] or "Ссылка"


def normalize_target_for_open(target: str) -> str:
    target = target.strip().strip('"')
    if target.startswith("file://"):
        parsed = urlparse(target)
        decoded_path = unquote(parsed.path)
        if parsed.netloc:
            target = f"//{parsed.netloc}{decoded_path}"
        else:
            target = decoded_path

    if target.startswith("////"):
        target = "//" + target.lstrip("/")

    if os.name == "nt" and target.startswith("//"):
        target = "\\\\" + target[2:].replace("/", "\\")

    return target


def open_target(target: str) -> None:
    normalized = normalize_target_for_open(target)
    if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", normalized):
        webbrowser.open(normalized)
        return

    if os.name == "nt":
        os.startfile(normalized)  # type: ignore[attr-defined]
        return

    if sys.platform == "darwin":
        subprocess.Popen(["open", normalized])
    else:
        subprocess.Popen(["xdg-open", normalized])


class TaskJournalHandler(BaseHTTPRequestHandler):
    server_version = "TaskJournal/0.1"

    def log_message(self, fmt: str, *args) -> None:
        message = fmt % args
        print(f"[{datetime.now().strftime('%H:%M:%S')}] {self.address_string()} {message}")

    def do_GET(self) -> None:
        try:
            parsed = urlparse(self.path)
            path = parsed.path
            if path.startswith("/api/"):
                self.route_api_get(path, parse_qs(parsed.query))
                return
            self.serve_static(path)
        except Exception as exc:
            error_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def do_POST(self) -> None:
        self.route_write("POST")

    def do_PUT(self) -> None:
        self.route_write("PUT")

    def do_DELETE(self) -> None:
        self.route_write("DELETE")

    def route_write(self, method: str) -> None:
        try:
            parsed = urlparse(self.path)
            if not parsed.path.startswith("/api/"):
                error_response(self, HTTPStatus.NOT_FOUND, "Не найдено")
                return
            data = {} if method == "DELETE" else read_json(self)
            with WRITE_LOCK:
                self.route_api_write(method, parsed.path, data)
        except ValueError as exc:
            error_response(self, HTTPStatus.BAD_REQUEST, str(exc))
        except sqlite3.IntegrityError as exc:
            error_response(self, HTTPStatus.BAD_REQUEST, f"Ошибка базы данных: {exc}")
        except FileNotFoundError as exc:
            error_response(self, HTTPStatus.NOT_FOUND, f"Не удалось открыть: {exc}")
        except Exception as exc:
            error_response(self, HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    def serve_static(self, path: str) -> None:
        if path in ("", "/"):
            file_path = STATIC_DIR / "index.html"
        elif path.startswith("/static/"):
            file_path = STATIC_DIR / path[len("/static/") :]
        else:
            file_path = STATIC_DIR / "index.html"

        resolved = file_path.resolve()
        if STATIC_DIR.resolve() not in resolved.parents and resolved != (STATIC_DIR / "index.html").resolve():
            error_response(self, HTTPStatus.FORBIDDEN, "Запрещенный путь")
            return
        if not resolved.exists() or not resolved.is_file():
            error_response(self, HTTPStatus.NOT_FOUND, "Файл не найден")
            return

        suffix = resolved.suffix.lower()
        content_type = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".svg": "image/svg+xml",
            ".png": "image/png",
        }.get(suffix, "application/octet-stream")

        body = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def route_api_get(self, path: str, query: dict) -> None:
        parts = path.strip("/").split("/")
        if path == "/api/bootstrap":
            self.get_bootstrap()
        elif path == "/api/tasks":
            self.get_tasks(query)
        elif len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            self.get_task(int(parts[2]))
        elif path == "/api/export":
            self.get_export()
        else:
            error_response(self, HTTPStatus.NOT_FOUND, "API не найден")

    def route_api_write(self, method: str, path: str, data: dict) -> None:
        parts = path.strip("/").split("/")

        if method == "POST" and path == "/api/projects":
            self.create_project(data)
        elif method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
            self.update_project(int(parts[2]), data)
        elif method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "projects"]:
            self.delete_project(int(parts[2]))
        elif method == "POST" and path == "/api/tasks":
            self.create_task(data)
        elif method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            self.update_task(int(parts[2]), data)
        elif method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "tasks"]:
            self.delete_task(int(parts[2]))
        elif method == "POST" and path == "/api/links":
            self.create_link(data)
        elif method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "links"]:
            self.update_link(int(parts[2]), data)
        elif method == "DELETE" and len(parts) == 3 and parts[:2] == ["api", "links"]:
            self.delete_link(int(parts[2]))
        elif method == "POST" and path == "/api/link-types":
            self.create_link_type(data)
        elif method == "PUT" and len(parts) == 3 and parts[:2] == ["api", "link-types"]:
            self.update_link_type(int(parts[2]), data)
        elif method == "POST" and path == "/api/open-link":
            self.open_link(data)
        elif method == "POST" and path == "/api/backup":
            self.create_backup()
        else:
            error_response(self, HTTPStatus.NOT_FOUND, "API не найден")

    def get_bootstrap(self) -> None:
        with db_connect() as conn:
            projects = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT p.*,
                        (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status != 'archive') AS active_count,
                        (SELECT COUNT(*) FROM tasks t WHERE t.project_id = p.id AND t.status = 'archive') AS archive_count
                    FROM projects p
                    ORDER BY p.sort_order, p.name
                    """
                )
            ]
            link_types = [
                dict(row)
                for row in conn.execute("SELECT * FROM link_types ORDER BY name COLLATE NOCASE")
            ]
        json_response(self, {"projects": projects, "linkTypes": link_types, "statuses": STATUSES})

    def get_tasks(self, query: dict) -> None:
        project_id = int(query.get("project_id", ["0"])[0] or 0)
        if not project_id:
            raise ValueError("project_id обязателен")

        with db_connect() as conn:
            rows = conn.execute(
                """
                SELECT t.*,
                    COUNT(l.id) AS link_count
                FROM tasks t
                LEFT JOIN links l ON l.task_id = t.id
                WHERE t.project_id = ?
                GROUP BY t.id
                ORDER BY
                    CASE t.status
                        WHEN 'current' THEN 1
                        WHEN 'next' THEN 2
                        WHEN 'waiting' THEN 3
                        WHEN 'done' THEN 4
                        WHEN 'archive' THEN 5
                        ELSE 6
                    END,
                    t.updated_at DESC,
                    t.id DESC
                """,
                (project_id,),
            )
            tasks = [dict(row) for row in rows]
        json_response(self, {"tasks": tasks})

    def get_task(self, task_id: int) -> None:
        with db_connect() as conn:
            task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
            if not task:
                error_response(self, HTTPStatus.NOT_FOUND, "Задача не найдена")
                return
            links = [
                dict(row)
                for row in conn.execute(
                    """
                    SELECT l.*, lt.name AS type_name, lt.color AS type_color
                    FROM links l
                    LEFT JOIN link_types lt ON lt.id = l.type_id
                    WHERE l.task_id = ?
                    ORDER BY COALESCE(lt.name, ''), l.label
                    """,
                    (task_id,),
                )
            ]
        json_response(self, {"task": task, "links": links})

    def create_project(self, data: dict) -> None:
        name = require_text(data, "name", "Название")
        description = optional_text(data, "description")
        stamp = now()
        with db_connect() as conn:
            next_order = conn.execute("SELECT COALESCE(MAX(sort_order), 0) + 1 FROM projects").fetchone()[0]
            cur = conn.execute(
                """
                INSERT INTO projects (name, description, sort_order, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, description, next_order, stamp, stamp),
            )
            conn.commit()
            project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)).fetchone())
        json_response(self, {"project": project}, HTTPStatus.CREATED)

    def update_project(self, project_id: int, data: dict) -> None:
        allowed = {
            "name": str,
            "description": str,
            "sort_order": int,
            "last_task_id": int_or_none,
        }
        updates = []
        values = []
        for key, caster in allowed.items():
            if key in data:
                value = caster(data[key])
                if key == "name" and not str(value).strip():
                    raise ValueError("Название проекта обязательно")
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            raise ValueError("Нет данных для обновления")
        updates.append("updated_at = ?")
        values.append(now())
        values.append(project_id)

        with db_connect() as conn:
            conn.execute(f"UPDATE projects SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()
            project = row_to_dict(conn.execute("SELECT * FROM projects WHERE id = ?", (project_id,)).fetchone())
        if not project:
            error_response(self, HTTPStatus.NOT_FOUND, "Проект не найден")
            return
        json_response(self, {"project": project})

    def delete_project(self, project_id: int) -> None:
        with db_connect() as conn:
            task_count = conn.execute("SELECT COUNT(*) FROM tasks WHERE project_id = ?", (project_id,)).fetchone()[0]
            if task_count:
                raise ValueError("Сначала удалите или перенесите задачи проекта")
            conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            conn.commit()
        json_response(self, {"ok": True})

    def create_task(self, data: dict) -> None:
        project_id = int(data.get("project_id") or 0)
        if not project_id:
            raise ValueError("project_id обязателен")
        title = require_text(data, "title", "Название")
        status = optional_text(data, "status") or "current"
        if status not in {item["key"] for item in STATUSES}:
            status = "current"
        stamp = now()
        with db_connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO tasks (project_id, title, status, summary, next_step, notes, created_at, updated_at, archived_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    project_id,
                    title,
                    status,
                    optional_text(data, "summary"),
                    optional_text(data, "next_step"),
                    optional_text(data, "notes"),
                    stamp,
                    stamp,
                    stamp if status == "archive" else None,
                ),
            )
            task_id = cur.lastrowid
            conn.execute(
                "UPDATE projects SET last_task_id = ?, updated_at = ? WHERE id = ?",
                (task_id, stamp, project_id),
            )
            conn.commit()
            task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        json_response(self, {"task": task}, HTTPStatus.CREATED)

    def update_task(self, task_id: int, data: dict) -> None:
        allowed = {
            "project_id": int,
            "title": str,
            "status": str,
            "summary": str,
            "next_step": str,
            "notes": str,
        }
        updates = []
        values = []
        new_status = data.get("status")
        for key, caster in allowed.items():
            if key in data:
                value = caster(data[key])
                if key == "title" and not str(value).strip():
                    raise ValueError("Название задачи обязательно")
                if key == "status" and value not in {item["key"] for item in STATUSES}:
                    raise ValueError("Неизвестный статус")
                updates.append(f"{key} = ?")
                values.append(value)

        if new_status == "archive":
            updates.append("archived_at = COALESCE(archived_at, ?)")
            values.append(now())
        elif new_status is not None:
            updates.append("archived_at = NULL")

        if not updates:
            raise ValueError("Нет данных для обновления")

        stamp = now()
        updates.append("updated_at = ?")
        values.append(stamp)
        values.append(task_id)

        with db_connect() as conn:
            conn.execute(f"UPDATE tasks SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()
            task = row_to_dict(conn.execute("SELECT * FROM tasks WHERE id = ?", (task_id,)).fetchone())
        if not task:
            error_response(self, HTTPStatus.NOT_FOUND, "Задача не найдена")
            return
        json_response(self, {"task": task})

    def delete_task(self, task_id: int) -> None:
        with db_connect() as conn:
            conn.execute("DELETE FROM tasks WHERE id = ?", (task_id,))
            conn.commit()
        json_response(self, {"ok": True})

    def create_link(self, data: dict) -> None:
        task_id = int(data.get("task_id") or 0)
        if not task_id:
            raise ValueError("task_id обязателен")
        target = require_text(data, "target", "Путь или ссылка")
        label = optional_text(data, "label") or derive_label(target)
        type_id = int_or_none(data.get("type_id"))
        notes = optional_text(data, "notes")
        stamp = now()

        with db_connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO links (task_id, label, target, type_id, notes, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (task_id, label, target, type_id, notes, stamp, stamp),
            )
            conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, task_id))
            conn.commit()
            link = row_to_dict(conn.execute("SELECT * FROM links WHERE id = ?", (cur.lastrowid,)).fetchone())
        json_response(self, {"link": link}, HTTPStatus.CREATED)

    def update_link(self, link_id: int, data: dict) -> None:
        allowed = {
            "label": str,
            "target": str,
            "type_id": int_or_none,
            "notes": str,
        }
        updates = []
        values = []
        for key, caster in allowed.items():
            if key in data:
                value = caster(data[key])
                if key in ("label", "target") and not str(value).strip():
                    raise ValueError("Название и путь ссылки обязательны")
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            raise ValueError("Нет данных для обновления")
        stamp = now()
        updates.append("updated_at = ?")
        values.append(stamp)
        values.append(link_id)
        with db_connect() as conn:
            conn.execute(f"UPDATE links SET {', '.join(updates)} WHERE id = ?", values)
            row = conn.execute("SELECT task_id FROM links WHERE id = ?", (link_id,)).fetchone()
            if row:
                conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (stamp, row["task_id"]))
            conn.commit()
            link = row_to_dict(conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone())
        if not link:
            error_response(self, HTTPStatus.NOT_FOUND, "Ссылка не найдена")
            return
        json_response(self, {"link": link})

    def delete_link(self, link_id: int) -> None:
        with db_connect() as conn:
            row = conn.execute("SELECT task_id FROM links WHERE id = ?", (link_id,)).fetchone()
            conn.execute("DELETE FROM links WHERE id = ?", (link_id,))
            if row:
                conn.execute("UPDATE tasks SET updated_at = ? WHERE id = ?", (now(), row["task_id"]))
            conn.commit()
        json_response(self, {"ok": True})

    def create_link_type(self, data: dict) -> None:
        name = require_text(data, "name", "Название типа")
        color = optional_text(data, "color") or "#59636e"
        if not re.match(r"^#[0-9a-fA-F]{6}$", color):
            color = "#59636e"
        stamp = now()
        with db_connect() as conn:
            cur = conn.execute(
                "INSERT INTO link_types (name, color, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (name, color, stamp, stamp),
            )
            conn.commit()
            link_type = row_to_dict(conn.execute("SELECT * FROM link_types WHERE id = ?", (cur.lastrowid,)).fetchone())
        json_response(self, {"linkType": link_type}, HTTPStatus.CREATED)

    def update_link_type(self, type_id: int, data: dict) -> None:
        allowed = {"name": str, "color": str}
        updates = []
        values = []
        for key, caster in allowed.items():
            if key in data:
                value = caster(data[key]).strip()
                if key == "name" and not value:
                    raise ValueError("Название типа обязательно")
                if key == "color" and not re.match(r"^#[0-9a-fA-F]{6}$", value):
                    raise ValueError("Цвет должен быть в формате #RRGGBB")
                updates.append(f"{key} = ?")
                values.append(value)
        if not updates:
            raise ValueError("Нет данных для обновления")
        updates.append("updated_at = ?")
        values.append(now())
        values.append(type_id)
        with db_connect() as conn:
            conn.execute(f"UPDATE link_types SET {', '.join(updates)} WHERE id = ?", values)
            conn.commit()
            link_type = row_to_dict(conn.execute("SELECT * FROM link_types WHERE id = ?", (type_id,)).fetchone())
        if not link_type:
            error_response(self, HTTPStatus.NOT_FOUND, "Тип ссылки не найден")
            return
        json_response(self, {"linkType": link_type})

    def open_link(self, data: dict) -> None:
        link_id = int(data.get("link_id") or 0)
        if not link_id:
            raise ValueError("link_id обязателен")
        with db_connect() as conn:
            link = conn.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if not link:
            error_response(self, HTTPStatus.NOT_FOUND, "Ссылка не найдена")
            return
        open_target(link["target"])
        json_response(self, {"ok": True, "target": link["target"]})

    def create_backup(self) -> None:
        BACKUP_DIR.mkdir(exist_ok=True)
        stamp = datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        backup_path = BACKUP_DIR / f"tasks-{stamp}.sqlite"
        shutil.copy2(DB_PATH, backup_path)
        json_response(self, {"ok": True, "path": str(backup_path)})

    def get_export(self) -> None:
        with db_connect() as conn:
            payload = {
                "exported_at": now(),
                "projects": [dict(row) for row in conn.execute("SELECT * FROM projects ORDER BY sort_order, name")],
                "tasks": [dict(row) for row in conn.execute("SELECT * FROM tasks ORDER BY project_id, updated_at DESC")],
                "linkTypes": [dict(row) for row in conn.execute("SELECT * FROM link_types ORDER BY name")],
                "links": [dict(row) for row in conn.execute("SELECT * FROM links ORDER BY task_id, label")],
            }
        json_response(self, payload)


def make_server(host: str, port: int) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), TaskJournalHandler)


def main() -> None:
    parser = argparse.ArgumentParser(description="Локальный журнал задач")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес сервера")
    parser.add_argument("--port", type=int, default=8787, help="Порт сервера")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    args = parser.parse_args()

    init_db()

    server = None
    port = args.port
    for candidate in range(args.port, args.port + 20):
        try:
            server = make_server(args.host, candidate)
            port = candidate
            break
        except OSError:
            continue
    if server is None:
        raise SystemExit("Не удалось найти свободный порт")

    url = f"http://{args.host}:{port}"
    print(f"Журнал задач запущен: {url}")
    print("Для остановки нажмите Ctrl+C")
    if not args.no_browser:
        threading.Timer(0.6, lambda: webbrowser.open(url)).start()

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nОстановка...")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
