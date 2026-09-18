#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
================================================================================
 ARNESEN'S COMPREHENSIVE SCHOOL — ERP BACKEND
 Single-file Flask + SQLAlchemy + Backblaze B2 (S3-compatible) backend.

 This file is intentionally monolithic (by explicit requirement): every piece
 of backend functionality — models, auth, permissions, business logic, file
 storage, backups, sync and IndexedDB migration — lives in app.py.

 It is designed to sit behind the existing index.html (which currently
 persists everything to IndexedDB). The server database becomes the
 authoritative store; IndexedDB becomes a local cache / offline layer that
 talks to this API instead of (or in addition to) its own local copy.

 Run:
     pip install -r requirements.txt
     python app.py
================================================================================
"""

import os
import re
import json
import gzip
import time
import random
import logging
import secrets
import mimetypes
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (
    Flask, request, jsonify, session, send_from_directory, g, abort, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, DateTime, Text, JSON,
    Index, or_, and_, func
)
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base
from sqlalchemy.exc import IntegrityError

from dotenv import load_dotenv

# boto3 is optional at import time so the app can still boot (e.g. for local
# dev without file storage configured) but every B2-touching route will
# return a clear 503 if it isn't installed / configured.
try:
    import boto3
    from botocore.client import Config as BotoConfig
    from botocore.exceptions import ClientError, BotoCoreError
    BOTO3_AVAILABLE = True
except Exception:  # pragma: no cover
    BOTO3_AVAILABLE = False


# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

load_dotenv()

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def env(name, default=None):
    val = os.environ.get(name, default)
    return val

SECRET_KEY = env("SECRET_KEY", "change-this-in-production")
DATABASE_URL = env("DATABASE_URL", "sqlite:///school_erp.db")

B2_KEY_ID = env("B2_KEY_ID")
B2_APPLICATION_KEY = env("B2_APPLICATION_KEY")
B2_BUCKET_NAME = env("B2_BUCKET_NAME")
B2_ENDPOINT = env("B2_ENDPOINT")

SESSION_LIFETIME_MINUTES = int(env("SESSION_LIFETIME_MINUTES", "480"))  # 8 hours
LOGIN_MAX_ATTEMPTS = int(env("LOGIN_MAX_ATTEMPTS", "5"))
LOGIN_LOCKOUT_MINUTES = int(env("LOGIN_LOCKOUT_MINUTES", "15"))

MAX_UPLOAD_MB = int(env("MAX_UPLOAD_MB", "25"))
ALLOWED_UPLOAD_EXTENSIONS = {
    "png", "jpg", "jpeg", "gif", "webp", "pdf", "doc", "docx", "xls", "xlsx",
    "csv", "txt", "zip",
}

CORS_ALLOWED_ORIGIN = env("CORS_ALLOWED_ORIGIN")  # optional, same-origin by default

FRONTEND_DIR = env("FRONTEND_DIR", BASE_DIR)
FRONTEND_INDEX = env("FRONTEND_INDEX", "index.html")


# ==============================================================================
# 2. LOGGING  (never log secrets, passwords, tokens)
# ==============================================================================

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
log = logging.getLogger("arnesens.erp")

_SECRET_PATTERNS = [
    re.compile(r"B2_APPLICATION_KEY", re.I),
    re.compile(r"password", re.I),
    re.compile(r"session", re.I),
    re.compile(r"token", re.I),
]

def safe_log_error(context, exc):
    """Log an error without ever leaking secret-looking values."""
    msg = str(exc)
    for pat in _SECRET_PATTERNS:
        if pat.search(msg):
            msg = "[REDACTED — potential secret in error message]"
            break
    log.error("%s: %s", context, msg)


# ==============================================================================
# 3. FLASK APP + DATABASE SETUP
# ==============================================================================

app = Flask(__name__, static_folder=None)
app.config.update(
    SECRET_KEY=SECRET_KEY,
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=env("SESSION_COOKIE_SECURE", "0") == "1",
    PERMANENT_SESSION_LIFETIME=timedelta(minutes=SESSION_LIFETIME_MINUTES),
    MAX_CONTENT_LENGTH=MAX_UPLOAD_MB * 1024 * 1024,
    JSON_SORT_KEYS=False,
)

engine_kwargs = {}
if DATABASE_URL.startswith("sqlite"):
    engine_kwargs["connect_args"] = {"check_same_thread": False}
engine = create_engine(DATABASE_URL, **engine_kwargs)
SessionLocal = scoped_session(sessionmaker(bind=engine, autoflush=False, autocommit=False))
Base = declarative_base()


@app.teardown_appcontext
def remove_session(exception=None):
    SessionLocal.remove()


# ==============================================================================
# 4. MODELS
# ------------------------------------------------------------------------------
# Two families of tables:
#
#  (a) `User` — a dedicated, tightly-typed table for authentication-critical
#      data (never stores plaintext passwords; only salted hashes).
#
#  (b) `Record` — a generic, indexed document table that holds every other
#      ERP entity (students, teachers, classes, subjects, exams, marks,
#      attendance, feeStructures, feePayments, grading, cbcGrading,
#      announcements, books, borrows, timetable, timetableRules, settings,
#      activity, discipline, inventoryItems, suppliers, stockMovements,
#      assets, leaveRequests, payrollRecords, appraisals, staffDocuments,
#      leaveOuts, teacherNotes). This mirrors the existing frontend's
#      IndexedDB object-store model 1:1 (each store is schema-flexible JSON
#      keyed by `id`), which is exactly what index.html already expects and
#      lets the server accept every field the UI sends without lockstep
#      migrations every time the frontend evolves a form. Common filter
#      fields (classId, studentId, teacherId, date, year, status, examId,
#      subjectId) are duplicated into indexed columns so list/filter/search
#      queries stay fast even with large data volumes.
#
#      Because both tables are plain SQL tables with indexed columns, moving
#      DATABASE_URL from sqlite:/// to a postgresql:// URL requires no
#      frontend API changes whatsoever — SQLAlchemy + JSON columns work
#      identically on both engines.
# ==============================================================================

STORES = [
    "teacherNotes", "students", "teachers", "classes", "subjects", "exams",
    "marks", "attendance", "feeStructures", "feePayments", "grading",
    "cbcGrading", "announcements", "books", "borrows", "timetable",
    "timetableRules", "settings", "activity", "discipline", "inventoryItems",
    "suppliers", "stockMovements", "assets", "leaveRequests",
    "payrollRecords", "appraisals", "staffDocuments", "leaveOuts", "files",
]


class User(Base):
    __tablename__ = "users"

    id = Column(String(64), primary_key=True)
    username = Column(String(120), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    name = Column(String(200), nullable=False)
    role = Column(String(60), nullable=False, index=True)
    active = Column(Boolean, default=True, nullable=False)
    photo = Column(Text, nullable=True)  # data URL or B2 key
    linked_teacher_id = Column(String(64), nullable=True, index=True)
    linked_student_ids = Column(JSON, default=list)
    must_change = Column(Boolean, default=False)
    failed_attempts = Column(Integer, default=0)
    locked_until = Column(DateTime, nullable=True)
    last_login_at = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    rev = Column(Integer, default=1, nullable=False)
    deleted = Column(Boolean, default=False, nullable=False)

    def to_public_dict(self):
        return {
            "id": self.id,
            "username": self.username,
            "name": self.name,
            "role": self.role,
            "active": self.active,
            "photo": self.photo,
            "linkedTeacherId": self.linked_teacher_id,
            "linkedStudentIds": self.linked_student_ids or [],
            "mustChange": self.must_change,
            "lockedUntil": self.locked_until.isoformat() if self.locked_until else None,
            "updatedAt": self.updated_at.isoformat() if self.updated_at else None,
            "rev": self.rev,
        }


class Record(Base):
    __tablename__ = "records"

    pk = Column(Integer, primary_key=True, autoincrement=True)
    id = Column(String(80), nullable=False, index=True)
    store = Column(String(40), nullable=False, index=True)
    data = Column(JSON, nullable=False, default=dict)

    # Denormalized filter columns (populated from `data` on every write).
    class_id = Column(String(80), nullable=True, index=True)
    student_id = Column(String(80), nullable=True, index=True)
    teacher_id = Column(String(80), nullable=True, index=True)
    subject_id = Column(String(80), nullable=True, index=True)
    exam_id = Column(String(80), nullable=True, index=True)
    item_id = Column(String(80), nullable=True, index=True)
    book_id = Column(String(80), nullable=True, index=True)
    rec_date = Column(String(20), nullable=True, index=True)
    year = Column(String(20), nullable=True, index=True)
    status = Column(String(40), nullable=True, index=True)

    rev = Column(Integer, default=1, nullable=False)
    deleted = Column(Boolean, default=False, nullable=False, index=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow, index=True)
    updated_by = Column(String(120), nullable=True)

    __table_args__ = (
        Index("ix_records_store_id", "store", "id", unique=True),
        Index("ix_records_store_deleted", "store", "deleted"),
        Index("ix_records_store_updated", "store", "updated_at"),
    )

    def to_dict(self):
        d = dict(self.data or {})
        d["id"] = self.id
        d["_rev"] = self.rev
        d["_updatedAt"] = self.updated_at.isoformat() if self.updated_at else None
        d["_deleted"] = self.deleted
        return d


Base.metadata.create_all(engine)


def db():
    """Return the request-scoped SQLAlchemy session."""
    return SessionLocal()


# ==============================================================================
# 5. ID GENERATION / SMALL HELPERS
# ==============================================================================

def new_id(prefix="id"):
    return f"{prefix}_{int(time.time() * 1000):x}_{secrets.token_hex(4)}"


def now():
    return datetime.utcnow()


def parse_iso(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).replace(tzinfo=None)
    except Exception:
        return None


def is_valid_admission_no(s):
    return bool(re.fullmatch(r"\d{4}", s or ""))


def to_float(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def json_error(message, status=400, **extra):
    payload = {"error": message}
    payload.update(extra)
    return jsonify(payload), status


# ==============================================================================
# 6. ROLE / PERMISSION MODEL
#    Mirrors ROLE_MODULES + PRIVILEGED_STAFF_ROLES from the existing frontend
#    so access rules are identical between the two — enforced here, on the
#    server, not just hidden in the UI.
# ==============================================================================

ROLE_MODULES = {
    "Administrator": ["dashboard", "students", "teachers", "classes", "subjects", "exams",
                       "marks", "analysis", "reportcards", "attendance", "fees", "timetable",
                       "library", "inventory", "hr", "communication", "users", "settings",
                       "logs", "leaveouts", "documents", "notes"],
    "Head Teacher": ["dashboard", "exams", "marks", "analysis", "reportcards", "attendance",
                      "fees", "communication", "leaveouts", "documents", "notes"],
    "Deputy Head Teacher": ["dashboard", "attendance", "exams", "analysis", "discipline",
                             "communication", "leaveouts", "documents", "notes"],
    "Registrar": ["dashboard", "students", "communication", "leaveouts", "documents"],
    "Class Teacher": ["dashboard", "marks", "analysis", "reportcards", "attendance",
                       "communication", "leaveouts", "documents", "notes"],
    "Subject Teacher": ["dashboard", "marks", "analysis", "communication", "notes"],
    "Bursar": ["dashboard", "fees", "inventory", "communication", "documents"],
    "Student": ["portal"],
    "Parent": ["portal"],
}
PRIVILEGED_STAFF_ROLES = {"Administrator", "Head Teacher", "Deputy Head Teacher"}
TEACHER_ROLES = {"Subject Teacher", "Class Teacher"}
STUDENT_PORTAL_ROLES = {"Student", "Parent"}
ADMIN_ROLES = {"Administrator"}

# Maps each generic store to the module that gates access to it.
STORE_MODULE = {
    "students": "students", "teachers": "teachers", "classes": "classes",
    "subjects": "subjects", "exams": "exams", "marks": "marks",
    "attendance": "attendance", "feeStructures": "fees", "feePayments": "fees",
    "grading": "settings", "cbcGrading": "settings", "announcements": "communication",
    "books": "library", "borrows": "library", "timetable": "timetable",
    "timetableRules": "timetable", "settings": "settings", "activity": "logs",
    "discipline": "discipline", "inventoryItems": "inventory", "suppliers": "inventory",
    "stockMovements": "inventory", "assets": "inventory", "leaveRequests": "hr",
    "payrollRecords": "hr", "appraisals": "hr", "staffDocuments": "documents",
    "leaveOuts": "leaveouts", "teacherNotes": "notes", "files": "documents",
}
# Stores that even a module-permitted role may only ever READ, never write to
# via the generic endpoints (writes happen through dedicated business routes
# or are administrator-only).
WRITE_RESTRICTED_STORES = {"activity"}
ADMIN_ONLY_WRITE_STORES = {"settings", "grading", "cbcGrading", "classes", "subjects"}
# Reference data every authenticated role needs to read (grading scales for
# report cards/analysis, school info for headers/receipts) even though only
# Administrators may write to it — so GET bypasses the module gate below.
PUBLIC_READ_STORES = {"settings", "grading", "cbcGrading"}

# Stores that must be scoped down to "your own children" for Student/Parent
# portal accounts, and to "your own classes" for Subject/Class Teachers.
STUDENT_SCOPED_STORES = {"students", "marks", "attendance", "feeStructures",
                          "feePayments", "discipline", "borrows", "leaveOuts"}
TEACHER_SCOPED_STORES = {"students", "marks", "attendance", "teacherNotes", "discipline"}


def get_grading_permissions_map():
    """settings store may hold an id='permissions' record overriding ROLE_MODULES."""
    rec = db().query(Record).filter_by(store="settings", id="permissions", deleted=False).first()
    if rec and isinstance(rec.data, dict):
        return rec.data
    return None


def role_has_module(role, module):
    override = get_grading_permissions_map()
    if override and role in override:
        return module in (override.get(role) or [])
    return module in ROLE_MODULES.get(role, [])


# ==============================================================================
# 7. AUTH — sessions, login/logout/me, CSRF, decorators
# ==============================================================================

def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    if getattr(g, "_user_cache", None) and g._user_cache.id == uid:
        return g._user_cache
    u = db().query(User).filter_by(id=uid, deleted=False).first()
    g._user_cache = u
    return u


def login_required(fn):
    @wraps(fn)
    def wrapper(*args, **kwargs):
        u = current_user()
        if not u or not u.active:
            return json_error("Authentication required.", 401)
        return fn(*args, **kwargs)
    return wrapper


def csrf_protect(fn):
    """Double-submit cookie/session CSRF check for state-changing requests."""
    @wraps(fn)
    def wrapper(*args, **kwargs):
        if request.method in ("POST", "PUT", "PATCH", "DELETE"):
            token = request.headers.get("X-CSRF-Token")
            expected = session.get("csrf_token")
            if not expected or not token or not secrets.compare_digest(token, expected):
                return json_error("Invalid or missing CSRF token.", 403)
        return fn(*args, **kwargs)
    return wrapper


def require_module(module):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u or not u.active:
                return json_error("Authentication required.", 401)
            if not role_has_module(u.role, module):
                return json_error("You do not have access to this module.", 403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


def require_roles(*roles):
    def deco(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            u = current_user()
            if not u or not u.active:
                return json_error("Authentication required.", 401)
            if u.role not in roles:
                return json_error("You do not have permission to perform this action.", 403)
            return fn(*args, **kwargs)
        return wrapper
    return deco


@app.route("/api/login", methods=["POST"])
def api_login():
    payload = request.get_json(silent=True) or {}
    username = (payload.get("username") or "").strip().lower()
    password = payload.get("password") or ""
    if not username or not password:
        return json_error("Username and password are required.", 400)

    s = db()
    user = s.query(User).filter(func.lower(User.username) == username, User.deleted == False).first()  # noqa: E712

    if user and user.locked_until and user.locked_until > now():
        remaining = int((user.locked_until - now()).total_seconds() // 60) + 1
        return json_error(f"Account locked. Try again in {remaining} minute(s).", 423)

    if not user or not user.active or not check_password_hash(user.password_hash, password):
        if user:
            user.failed_attempts = (user.failed_attempts or 0) + 1
            if user.failed_attempts >= LOGIN_MAX_ATTEMPTS:
                user.locked_until = now() + timedelta(minutes=LOGIN_LOCKOUT_MINUTES)
                user.failed_attempts = 0
            s.commit()
        return json_error("Invalid username or password.", 401)

    user.failed_attempts = 0
    user.locked_until = None
    user.last_login_at = now()
    s.commit()

    session.clear()
    session.permanent = True
    session["user_id"] = user.id
    session["role"] = user.role
    session["csrf_token"] = secrets.token_urlsafe(32)

    log_activity(f"{user.name} logged in", user)

    resp = user.to_public_dict()
    resp["csrfToken"] = session["csrf_token"]
    resp["modules"] = ROLE_MODULES.get(user.role, [])
    return jsonify(resp)


@app.route("/api/logout", methods=["POST"])
@login_required
def api_logout():
    u = current_user()
    if u:
        log_activity(f"{u.name} logged out", u)
    session.clear()
    return jsonify({"ok": True})


@app.route("/api/me", methods=["GET"])
def api_me():
    u = current_user()
    if not u:
        return json_error("Not authenticated.", 401)
    resp = u.to_public_dict()
    resp["csrfToken"] = session.get("csrf_token")
    resp["modules"] = ROLE_MODULES.get(u.role, [])
    return jsonify(resp)


@app.route("/api/change-password", methods=["POST"])
@login_required
@csrf_protect
def api_change_password():
    u = current_user()
    payload = request.get_json(silent=True) or {}
    old = payload.get("oldPassword") or ""
    new = payload.get("newPassword") or ""
    if not new or len(new) < 4:
        return json_error("New password must be at least 4 characters.", 400)
    if not check_password_hash(u.password_hash, old):
        return json_error("Current password is incorrect.", 401)
    s = db()
    u.password_hash = generate_password_hash(new)
    u.must_change = False
    u.rev = (u.rev or 1) + 1
    s.commit()
    return jsonify({"ok": True})


def log_activity(text, user=None):
    s = db()
    rec = Record(
        id=new_id("act"), store="activity",
        data={"text": text, "user": user.name if user else "System",
              "time": now().isoformat()},
        rec_date=now().date().isoformat(),
        updated_by=user.username if user else "system",
    )
    s.add(rec)
    s.commit()


# ==============================================================================
# 8. TEACHER / STUDENT SCOPING HELPERS
# ==============================================================================

def _record_data(store, rec_id):
    rec = db().query(Record).filter_by(store=store, id=rec_id, deleted=False).first()
    return rec.data if rec else None


def get_teacher_for_user(user):
    if not user or not user.linked_teacher_id:
        return None
    return _record_data("teachers", user.linked_teacher_id)


def teacher_visible_class_ids(user):
    """Classes a teacher may see: classes they're assigned to teach, plus any
    class where they are the class teacher."""
    teacher = get_teacher_for_user(user)
    if not teacher:
        return set()
    ids = set(teacher.get("classIds") or [])
    s = db()
    own_classes = s.query(Record).filter_by(store="classes", deleted=False).all()
    for c in own_classes:
        if (c.data or {}).get("classTeacherId") == user.linked_teacher_id:
            ids.add(c.id)
    return ids


def teacher_can_access_class(user, class_id):
    if not class_id:
        return False
    return class_id in teacher_visible_class_ids(user)


def teacher_can_access_subject(user, subject_id, class_id):
    teacher = get_teacher_for_user(user)
    if not teacher:
        return False
    return subject_id in (teacher.get("subjectIds") or [])


def student_ids_for_portal_user(user):
    return set(user.linked_student_ids or [])


def student_class_id(student_id):
    d = _record_data("students", student_id)
    return (d or {}).get("classId")


# ==============================================================================
# 9. GENERIC RECORD CRUD ENGINE
# ==============================================================================

FILTER_COLUMNS = {
    "classId": "class_id", "studentId": "student_id", "teacherId": "teacher_id",
    "subjectId": "subject_id", "examId": "exam_id", "itemId": "item_id",
    "bookId": "book_id", "date": "rec_date", "year": "year", "status": "status",
}


def _extract_indexed_fields(data):
    return {
        "class_id": data.get("classId"),
        "student_id": data.get("studentId"),
        "teacher_id": data.get("teacherId"),
        "subject_id": data.get("subjectId"),
        "exam_id": data.get("examId"),
        "item_id": data.get("itemId"),
        "book_id": data.get("bookId"),
        "rec_date": str(data.get("date")) if data.get("date") else None,
        "year": str(data.get("year")) if data.get("year") else None,
        "status": data.get("status"),
    }


def create_record(store, data, record_id=None, user=None):
    s = db()
    rid = record_id or data.get("id") or new_id(store[:3])
    data = dict(data)
    data["id"] = rid
    existing = s.query(Record).filter_by(store=store, id=rid).first()
    fields = _extract_indexed_fields(data)
    if existing:
        existing.data = data
        existing.deleted = False
        existing.rev = (existing.rev or 1) + 1
        existing.updated_by = user.username if user else None
        for k, v in fields.items():
            setattr(existing, k, v)
        s.commit()
        return existing
    rec = Record(id=rid, store=store, data=data, rev=1,
                 updated_by=user.username if user else None, **fields)
    s.add(rec)
    s.commit()
    return rec


def update_record(store, record_id, patch, user=None, replace=False):
    s = db()
    rec = s.query(Record).filter_by(store=store, id=record_id, deleted=False).first()
    if not rec:
        return None
    new_data = dict(patch) if replace else {**(rec.data or {}), **patch}
    new_data["id"] = record_id
    rec.data = new_data
    rec.rev = (rec.rev or 1) + 1
    rec.updated_by = user.username if user else None
    for k, v in _extract_indexed_fields(new_data).items():
        setattr(rec, k, v)
    s.commit()
    return rec


def soft_delete_record(store, record_id, user=None):
    s = db()
    rec = s.query(Record).filter_by(store=store, id=record_id, deleted=False).first()
    if not rec:
        return False
    rec.deleted = True
    rec.rev = (rec.rev or 1) + 1
    rec.updated_by = user.username if user else None
    s.commit()
    return True


def query_store(store, args, scope_filter=None):
    s = db()
    q = s.query(Record).filter_by(store=store, deleted=False)
    for key, col in FILTER_COLUMNS.items():
        val = args.get(key)
        if val:
            q = q.filter(getattr(Record, col) == val)
    search = (args.get("q") or args.get("search") or "").strip().lower()
    date_from = args.get("dateFrom")
    date_to = args.get("dateTo")
    if date_from:
        q = q.filter(Record.rec_date >= date_from)
    if date_to:
        q = q.filter(Record.rec_date <= date_to)

    items = q.all()

    if search:
        def matches(rec):
            blob = json.dumps(rec.data, default=str).lower()
            return search in blob
        items = [r for r in items if matches(r)]

    if scope_filter:
        items = [r for r in items if scope_filter(r)]

    total = len(items)
    try:
        page = max(1, int(args.get("page", 1)))
        per_page = min(500, max(1, int(args.get("perPage", args.get("per_page", 0)) or 0)))
    except (TypeError, ValueError):
        page, per_page = 1, 0

    if per_page:
        start = (page - 1) * per_page
        items = items[start:start + per_page]

    return items, total


def build_scope_filter(store, user):
    """Return a function(record)->bool restricting visibility per role, or
    None if the role has unrestricted access to this store."""
    if user.role in STUDENT_PORTAL_ROLES and store in STUDENT_SCOPED_STORES:
        own_ids = student_ids_for_portal_user(user)
        own_classes = {student_class_id(sid) for sid in own_ids}
        if store == "students":
            return lambda r: r.id in own_ids
        if store in ("marks", "attendance", "feePayments", "discipline", "borrows", "leaveOuts"):
            return lambda r: (r.data or {}).get("studentId") in own_ids
        if store == "feeStructures":
            return lambda r: (r.data or {}).get("classId") in own_classes

    if user.role in TEACHER_ROLES and store in TEACHER_SCOPED_STORES and user.role not in PRIVILEGED_STAFF_ROLES:
        visible_classes = teacher_visible_class_ids(user)
        if store == "students":
            return lambda r: (r.data or {}).get("classId") in visible_classes
        if store in ("marks", "attendance"):
            def f(r):
                sid = (r.data or {}).get("studentId")
                cls = student_class_id(sid) if sid else (r.data or {}).get("classId")
                return cls in visible_classes
            return f
        if store == "teacherNotes":
            return lambda r: (r.data or {}).get("teacherId") == user.linked_teacher_id or (r.data or {}).get("classId") in visible_classes
        if store == "discipline":
            def f(r):
                sid = (r.data or {}).get("studentId")
                cls = student_class_id(sid) if sid else None
                return cls in visible_classes
            return f
    return None


# Public URL path for each internal store name. The frontend's IndexedDB
# object stores are camelCase (feeStructures, timetableRules, ...) but the
# REST API surface uses the hyphenated / shortened paths specified for this
# project (/api/fees, /api/fee-payments, /api/timetable-rules, /api/inventory,
# /api/stock-movements, /api/leave-requests, /api/payroll, /api/staff-documents,
# /api/teacher-notes, /api/cbc-grading, ...). This map is the single place
# that translates between the two.
STORE_URL_PATH = {
    "feeStructures": "fees",
    "feePayments": "fee-payments",
    "timetableRules": "timetable-rules",
    "inventoryItems": "inventory",
    "stockMovements": "stock-movements",
    "leaveRequests": "leave-requests",
    "payrollRecords": "payroll",
    "staffDocuments": "staff-documents",
    "teacherNotes": "teacher-notes",
    "cbcGrading": "cbc-grading",
    "leaveOuts": "leaveouts",
    "activity": "logs",
}


def store_url_path(store):
    return STORE_URL_PATH.get(store, store)


def register_crud(store):
    module = STORE_MODULE.get(store, store)
    path = store_url_path(store)

    @app.route(f"/api/{path}", methods=["GET"], endpoint=f"list_{store}")
    @login_required
    def _list(store=store, module=module):
        u = current_user()
        if store not in PUBLIC_READ_STORES and not role_has_module(u.role, module):
            return json_error("You do not have access to this module.", 403)
        scope = build_scope_filter(store, u)
        items, total = query_store(store, request.args, scope_filter=scope)
        return jsonify({
            "items": [r.to_dict() for r in items],
            "total": total,
            "page": int(request.args.get("page", 1)),
        })

    @app.route(f"/api/{path}", methods=["POST"], endpoint=f"create_{store}")
    @login_required
    @csrf_protect
    def _create(store=store, module=module):
        u = current_user()
        if not role_has_module(u.role, module):
            return json_error("You do not have access to this module.", 403)
        if store in WRITE_RESTRICTED_STORES:
            return json_error("This resource is read-only via the API.", 403)
        if store in ADMIN_ONLY_WRITE_STORES and u.role not in PRIVILEGED_STAFF_ROLES:
            return json_error("Only administrators may modify this resource.", 403)
        payload = request.get_json(silent=True) or {}
        err = validate_store_payload(store, payload, is_new=True)
        if err:
            return json_error(err, 400)
        rec = create_record(store, payload, user=u)
        log_activity(f"{u.name} created a {store[:-1] if store.endswith('s') else store} record ({rec.id})", u)
        return jsonify(rec.to_dict()), 201

    @app.route(f"/api/{path}/<record_id>", methods=["GET"], endpoint=f"get_{store}")
    @login_required
    def _get(record_id, store=store, module=module):
        u = current_user()
        if store not in PUBLIC_READ_STORES and not role_has_module(u.role, module):
            return json_error("You do not have access to this module.", 403)
        rec = db().query(Record).filter_by(store=store, id=record_id, deleted=False).first()
        if not rec:
            return json_error("Not found.", 404)
        scope = build_scope_filter(store, u)
        if scope and not scope(rec):
            return json_error("Not found.", 404)
        return jsonify(rec.to_dict())

    @app.route(f"/api/{path}/<record_id>", methods=["PUT", "PATCH"], endpoint=f"update_{store}")
    @login_required
    @csrf_protect
    def _update(record_id, store=store, module=module):
        u = current_user()
        if not role_has_module(u.role, module):
            return json_error("You do not have access to this module.", 403)
        if store in WRITE_RESTRICTED_STORES:
            return json_error("This resource is read-only via the API.", 403)
        if store in ADMIN_ONLY_WRITE_STORES and u.role not in PRIVILEGED_STAFF_ROLES:
            return json_error("Only administrators may modify this resource.", 403)
        existing = db().query(Record).filter_by(store=store, id=record_id, deleted=False).first()
        if not existing:
            return json_error("Not found.", 404)
        scope = build_scope_filter(store, u)
        if scope and not scope(existing):
            return json_error("Not found.", 404)
        payload = request.get_json(silent=True) or {}
        err = validate_store_payload(store, payload, is_new=False)
        if err:
            return json_error(err, 400)
        rec = update_record(store, record_id, payload, user=u, replace=(request.method == "PUT"))
        log_activity(f"{u.name} updated a {store[:-1] if store.endswith('s') else store} record ({record_id})", u)
        return jsonify(rec.to_dict())

    @app.route(f"/api/{path}/<record_id>", methods=["DELETE"], endpoint=f"delete_{store}")
    @login_required
    @csrf_protect
    def _delete(record_id, store=store, module=module):
        u = current_user()
        if not role_has_module(u.role, module):
            return json_error("You do not have access to this module.", 403)
        if store in WRITE_RESTRICTED_STORES:
            return json_error("This resource is read-only via the API.", 403)
        if store in ADMIN_ONLY_WRITE_STORES and u.role not in PRIVILEGED_STAFF_ROLES:
            return json_error("Only administrators may modify this resource.", 403)
        existing = db().query(Record).filter_by(store=store, id=record_id, deleted=False).first()
        if not existing:
            return json_error("Not found.", 404)
        scope = build_scope_filter(store, u)
        if scope and not scope(existing):
            return json_error("Not found.", 404)
        soft_delete_record(store, record_id, user=u)
        log_activity(f"{u.name} deleted a {store[:-1] if store.endswith('s') else store} record ({record_id})", u)
        return jsonify({"ok": True})


def validate_store_payload(store, payload, is_new):
    """Lightweight, store-specific required-field validation. Kept
    intentionally permissive (frontend already validates in the UI) — this
    is a defense-in-depth safety net, not the primary validator."""
    if store == "students":
        if is_new:
            if not payload.get("firstName") or not payload.get("lastName"):
                return "First and last name are required."
            adm = payload.get("admissionNo", "")
            if not is_valid_admission_no(adm):
                return "Admission number must be exactly 4 digits."
            s = db()
            existing_students = s.query(Record).filter_by(store="students", deleted=False).all()
            if any((r.data or {}).get("admissionNo") == adm for r in existing_students):
                return f"Admission number {adm} is already in use."
    elif store == "teachers":
        if is_new and (not payload.get("firstName") or not payload.get("lastName")):
            return "First and last name are required."
    elif store == "feeStructures":
        if not payload.get("classId") or not payload.get("year") or not to_float(payload.get("amount")):
            return "Class, academic year and a valid amount are required."
    elif store == "exams":
        if is_new and (not payload.get("name") or not payload.get("examDate")):
            return "Exam name and date are required."
    return None


# "files" has fully custom routes below (upload / signed-url access / delete)
# that intentionally live at the same paths a generic CRUD registration would
# use, so it is excluded here to avoid a routing collision.
for _store in STORES:
    if _store == "files":
        continue
    register_crud(_store)


# ==============================================================================
# 10. STUDENTS / TEACHERS — auto-provisioned portal logins
#     (mirrors autoCreateUserAccount() in the existing frontend)
# ==============================================================================

def unique_username(base):
    s = db()
    candidate = re.sub(r"[^a-z0-9]", "", (base or "user").lower().strip()) or "user"
    final, n = candidate, 1
    while s.query(User).filter(func.lower(User.username) == final).first():
        n += 1
        final = f"{candidate}{n}"
    return final


def derive_four_digit_pin(seed):
    seed = str(seed or "").strip()
    if re.fullmatch(r"\d{4}", seed):
        return seed
    return str(random.randint(1000, 9999))


def auto_create_user_account(name, username_base, password_seed, role,
                              linked_student_ids=None, linked_teacher_id=None):
    if not password_seed:
        return None, None
    s = db()
    pin = derive_four_digit_pin(password_seed)
    username = unique_username(username_base)
    user = User(
        id=new_id("u"), username=username, password_hash=generate_password_hash(pin),
        name=name, role=role, active=True,
        linked_teacher_id=linked_teacher_id, linked_student_ids=linked_student_ids or [],
    )
    s.add(user)
    s.commit()
    log_activity(f"Auto-created {role} user account for {name} (username: {username})")
    return user, pin


@app.route("/api/students/<student_id>/provision-login", methods=["POST"], endpoint="provision_student_login")
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES, "Registrar")
def provision_student_login(student_id):
    d = _record_data("students", student_id)
    if not d:
        return json_error("Student not found.", 404)
    existing = db().query(User).filter(User.linked_student_ids.isnot(None)).all()
    for u in existing:
        if student_id in (u.linked_student_ids or []):
            return json_error("This pupil already has a linked login.", 409)
    user, pin = auto_create_user_account(
        f"{d.get('firstName','')} {d.get('lastName','')}".strip(), d.get("firstName", "student"),
        d.get("admissionNo"), "Student", linked_student_ids=[student_id],
    )
    if not user:
        return json_error("Student record has no admission number to derive a password from.", 400)
    return jsonify({"user": user.to_public_dict(), "plainPassword": pin}), 201


@app.route("/api/teachers/<teacher_id>/provision-login", methods=["POST"], endpoint="provision_teacher_login")
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def provision_teacher_login(teacher_id):
    d = _record_data("teachers", teacher_id)
    if not d:
        return json_error("Teacher not found.", 404)
    if db().query(User).filter_by(linked_teacher_id=teacher_id).first():
        return json_error("This teacher already has a linked login.", 409)
    user, pin = auto_create_user_account(
        f"{d.get('firstName','')} {d.get('lastName','')}".strip(), d.get("firstName", "teacher"),
        d.get("staffNo"), "Subject Teacher", linked_teacher_id=teacher_id,
    )
    if not user:
        return json_error("Teacher record has no TSC/Staff No. to derive a password from.", 400)
    return jsonify({"user": user.to_public_dict(), "plainPassword": pin}), 201


# ==============================================================================
# 11. USERS & ROLES (Administrator only) — dedicated endpoints (not generic
#     CRUD, since this table has real auth semantics: password hashing,
#     lockouts, uniqueness).
# ==============================================================================

@app.route("/api/users", methods=["GET"])
@login_required
@require_roles("Administrator")
def list_users():
    s = db()
    q = s.query(User).filter_by(deleted=False)
    search = (request.args.get("q") or "").strip().lower()
    users = q.all()
    if search:
        users = [u for u in users if search in f"{u.name} {u.username} {u.role}".lower()]
    return jsonify({"items": [u.to_public_dict() for u in users], "total": len(users)})


@app.route("/api/users", methods=["POST"])
@login_required
@csrf_protect
@require_roles("Administrator")
def create_user():
    payload = request.get_json(silent=True) or {}
    name = (payload.get("name") or "").strip()
    username = (payload.get("username") or "").strip()
    role = payload.get("role")
    password = payload.get("password") or ""
    if not name or not username or not role:
        return json_error("Name, username and role are required.", 400)
    if role not in ROLE_MODULES:
        return json_error("Unknown role.", 400)
    if not password or len(password) < 4:
        return json_error("Password is required (min 4 characters).", 400)
    s = db()
    if s.query(User).filter(func.lower(User.username) == username.lower()).first():
        return json_error("Username already exists.", 409)
    user = User(
        id=new_id("u"), username=username, password_hash=generate_password_hash(password),
        name=name, role=role, active=True, photo=payload.get("photo"),
        linked_student_ids=payload.get("linkedStudentIds") or [],
        linked_teacher_id=payload.get("linkedTeacherId"),
    )
    s.add(user)
    s.commit()
    log_activity(f"{current_user().name} created user account for {name} ({role})", current_user())
    return jsonify(user.to_public_dict()), 201


@app.route("/api/users/<user_id>", methods=["PUT", "PATCH"])
@login_required
@csrf_protect
@require_roles("Administrator")
def update_user(user_id):
    s = db()
    user = s.query(User).filter_by(id=user_id, deleted=False).first()
    if not user:
        return json_error("Not found.", 404)
    payload = request.get_json(silent=True) or {}
    if "name" in payload:
        user.name = payload["name"].strip()
    if "role" in payload and payload["role"] in ROLE_MODULES:
        user.role = payload["role"]
    if "active" in payload:
        user.active = bool(payload["active"])
    if "photo" in payload:
        user.photo = payload["photo"]
    if "linkedStudentIds" in payload:
        user.linked_student_ids = payload["linkedStudentIds"] or []
    if payload.get("password"):
        if len(payload["password"]) < 4:
            return json_error("Password must be at least 4 characters.", 400)
        user.password_hash = generate_password_hash(payload["password"])
    user.rev = (user.rev or 1) + 1
    s.commit()
    log_activity(f"{current_user().name} updated user account for {user.name}", current_user())
    return jsonify(user.to_public_dict())


@app.route("/api/users/<user_id>", methods=["DELETE"])
@login_required
@csrf_protect
@require_roles("Administrator")
def delete_user(user_id):
    s = db()
    user = s.query(User).filter_by(id=user_id, deleted=False).first()
    if not user:
        return json_error("Not found.", 404)
    if user.username == "admin":
        return json_error("The primary admin account cannot be deleted.", 400)
    user.deleted = True
    user.active = False
    user.rev = (user.rev or 1) + 1
    s.commit()
    log_activity(f"{current_user().name} deleted user account for {user.name}", current_user())
    return jsonify({"ok": True})


@app.route("/api/users/<user_id>/unlock", methods=["POST"])
@login_required
@csrf_protect
@require_roles("Administrator")
def unlock_user(user_id):
    s = db()
    user = s.query(User).filter_by(id=user_id, deleted=False).first()
    if not user:
        return json_error("Not found.", 404)
    user.locked_until = None
    user.failed_attempts = 0
    s.commit()
    return jsonify({"ok": True})


# ==============================================================================
# 12. MARKS — batch entry (fast, single request, single transaction)
# ==============================================================================

@app.route("/api/marks/batch", methods=["POST"])
@login_required
@csrf_protect
def marks_batch():
    u = current_user()
    if not role_has_module(u.role, "marks"):
        return json_error("You do not have access to marks entry.", 403)

    payload = request.get_json(silent=True) or {}
    exam_id = payload.get("examId")
    class_id = payload.get("classId")
    subject_id = payload.get("subjectId")
    max_marks = to_float(payload.get("maxMarks"), 100)
    entries = payload.get("entries") or []

    if not exam_id or not subject_id or not entries:
        return json_error("examId, subjectId and at least one entry are required.", 400)

    if u.role in TEACHER_ROLES and u.role not in PRIVILEGED_STAFF_ROLES:
        if not teacher_can_access_class(u, class_id) or not teacher_can_access_subject(u, subject_id, class_id):
            return json_error("You are not assigned to this class/subject.", 403)

    s = db()
    saved, errors = [], []
    try:
        for entry in entries:
            student_id = entry.get("studentId")
            if not student_id:
                errors.append({"entry": entry, "error": "Missing studentId"})
                continue
            is_absent = bool(entry.get("isAbsent"))
            score = entry.get("score")
            if not is_absent and score not in (None, ""):
                score = to_float(score)
                if score < 0:
                    score = 0
                if score > max_marks:
                    score = max_marks
            else:
                score = None

            existing = s.query(Record).filter_by(
                store="marks", deleted=False, exam_id=exam_id,
                student_id=student_id, subject_id=subject_id,
            ).first()

            if score is None and not is_absent:
                if existing:
                    s.delete(existing)
                continue

            data = {
                "examId": exam_id, "studentId": student_id, "subjectId": subject_id,
                "maxMarks": max_marks, "score": score, "isAbsent": is_absent,
            }
            if existing:
                data["id"] = existing.id
                existing.data = data
                existing.rev = (existing.rev or 1) + 1
                existing.updated_by = u.username
                for k, v in _extract_indexed_fields(data).items():
                    setattr(existing, k, v)
                saved.append(existing.id)
            else:
                rid = new_id("mk")
                data["id"] = rid
                rec = Record(id=rid, store="marks", data=data, rev=1,
                             updated_by=u.username, **_extract_indexed_fields(data))
                s.add(rec)
                saved.append(rid)
        s.commit()
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("marks_batch failed", exc)
        return json_error("Failed to save marks; no changes were applied.", 500)

    log_activity(f"{u.name} submitted {len(saved)} mark(s) for exam {exam_id}", u)
    return jsonify({"saved": len(saved), "errors": errors})


# ==============================================================================
# 13. ATTENDANCE — batch entry
# ==============================================================================

@app.route("/api/attendance/batch", methods=["POST"])
@login_required
@csrf_protect
def attendance_batch():
    u = current_user()
    if not role_has_module(u.role, "attendance"):
        return json_error("You do not have access to attendance.", 403)

    payload = request.get_json(silent=True) or {}
    att_date = payload.get("date")
    class_id = payload.get("classId")
    att_type = payload.get("type", "Student")
    entries = payload.get("entries") or []

    if not att_date or not entries:
        return json_error("date and at least one entry are required.", 400)

    if att_type == "Student" and u.role in TEACHER_ROLES and u.role not in PRIVILEGED_STAFF_ROLES:
        if not teacher_can_access_class(u, class_id):
            return json_error("You are not assigned to this class.", 403)

    s = db()
    saved = []
    try:
        for entry in entries:
            key_field = "studentId" if att_type == "Student" else "teacherId"
            key_val = entry.get(key_field)
            status = entry.get("status")
            if not key_val or not status:
                continue
            filt = {"store": "attendance", "deleted": False, "rec_date": att_date}
            if att_type == "Student":
                filt["student_id"] = key_val
            else:
                filt["teacher_id"] = key_val
            candidates = s.query(Record).filter_by(**filt).all()
            existing = next((r for r in candidates if (r.data or {}).get("type") == att_type), None)
            data = {key_field: key_val, "date": att_date, "status": status, "type": att_type}
            if existing:
                data["id"] = existing.id
                existing.data = data
                existing.rev = (existing.rev or 1) + 1
                existing.updated_by = u.username
                for k, v in _extract_indexed_fields(data).items():
                    setattr(existing, k, v)
                saved.append(existing.id)
            else:
                rid = new_id("att")
                data["id"] = rid
                rec = Record(id=rid, store="attendance", data=data, rev=1,
                             updated_by=u.username, **_extract_indexed_fields(data))
                s.add(rec)
                saved.append(rid)
        s.commit()
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("attendance_batch failed", exc)
        return json_error("Failed to save attendance; no changes were applied.", 500)

    log_activity(f"{u.name} recorded attendance for {len(saved)} record(s) on {att_date}", u)
    return jsonify({"saved": len(saved)})


# ==============================================================================
# 14. FEES — annual fee structures + payments + balances + receipts
# ==============================================================================

def fee_balance_for(student_id, year):
    s = db()
    student = _record_data("students", student_id)
    if not student:
        return None
    class_id = student.get("classId")
    structs = s.query(Record).filter_by(store="feeStructures", deleted=False, class_id=class_id, year=str(year)).all()
    billed = sum(to_float((r.data or {}).get("amount")) for r in structs)
    payments = s.query(Record).filter_by(store="feePayments", deleted=False, student_id=student_id).all()
    paid = sum(to_float((r.data or {}).get("amount")) for r in payments
               if not (r.data or {}).get("year") or str((r.data or {}).get("year")) == str(year))
    return {"billed": billed, "paid": paid, "balance": billed - paid, "year": str(year)}


@app.route("/api/fees/balance/<student_id>", methods=["GET"])
@login_required
def fee_balance(student_id):
    u = current_user()
    if not role_has_module(u.role, "fees") and student_id not in student_ids_for_portal_user(u):
        return json_error("You do not have access to this record.", 403)
    year = request.args.get("year") or current_school_year()
    bal = fee_balance_for(student_id, year)
    if bal is None:
        return json_error("Student not found.", 404)
    return jsonify(bal)


def current_school_year():
    settings = _record_data("settings", "school") or {}
    return settings.get("currentYear") or str(date.today().year)


def next_receipt_no():
    count = db().query(Record).filter_by(store="feePayments").count()
    return f"RCT/{date.today().year}/{count + 1:05d}"


@login_required
@csrf_protect
def create_fee_payment():
    u = current_user()
    if not role_has_module(u.role, "fees"):
        return json_error("You do not have access to fees.", 403)
    payload = request.get_json(silent=True) or {}
    student_id = payload.get("studentId")
    amount = to_float(payload.get("amount"))
    if not student_id or amount <= 0:
        return json_error("studentId and a positive amount are required.", 400)
    year = payload.get("year") or current_school_year()

    s = db()
    try:
        bal_before = fee_balance_for(student_id, year)
        if bal_before is None:
            return json_error("Student not found.", 404)
        rid = new_id("fp")
        data = {
            "id": rid, "studentId": student_id, "amount": amount, "year": year,
            "date": payload.get("date") or date.today().isoformat(),
            "time": now().isoformat(), "method": payload.get("method", "Cash"),
            "ref": payload.get("ref", ""), "remarks": payload.get("remarks", ""),
            "receiptNo": next_receipt_no(), "cashier": u.name,
            "balBefore": bal_before["balance"], "balAfter": bal_before["balance"] - amount,
        }
        rec = Record(id=rid, store="feePayments", data=data, rev=1,
                     updated_by=u.username, **_extract_indexed_fields(data))
        s.add(rec)
        s.commit()
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("create_fee_payment failed", exc)
        return json_error("Failed to record payment; no changes were applied.", 500)

    log_activity(f"{u.name} recorded a fee payment of {amount} for student {student_id}", u)
    return jsonify(rec.to_dict()), 201


# Overwrite the generic feePayments POST route with the business-logic version above.
app.view_functions["create_feePayments"] = create_fee_payment


# ==============================================================================
# 15. TIMETABLE — group-level rules (Lower Primary / JSS) + per-class grid
# ==============================================================================

@app.route("/api/timetable-rules/group/<division>", methods=["GET"])
@login_required
def timetable_rules_for_division(division):
    u = current_user()
    if not role_has_module(u.role, "timetable"):
        return json_error("You do not have access to timetable.", 403)
    class_id = request.args.get("classId")
    s = db()
    q = s.query(Record).filter_by(store="timetableRules", deleted=False)
    rows = [r for r in q.all() if (r.data or {}).get("division") == division]
    group_rules = [r.to_dict() for r in rows if not (r.data or {}).get("classId")]
    class_overrides = [r.to_dict() for r in rows if class_id and (r.data or {}).get("classId") == class_id]
    return jsonify({"groupRules": group_rules, "classOverrides": class_overrides})


# ==============================================================================
# 16. BACKBLAZE B2 FILE STORAGE — private bucket, signed URLs, never expose keys
# ==============================================================================

_b2_client = None

def b2_client():
    global _b2_client
    if not BOTO3_AVAILABLE:
        return None
    if not (B2_KEY_ID and B2_APPLICATION_KEY and B2_BUCKET_NAME and B2_ENDPOINT):
        return None
    if _b2_client is None:
        _b2_client = boto3.client(
            "s3",
            endpoint_url=B2_ENDPOINT,
            aws_access_key_id=B2_KEY_ID,
            aws_secret_access_key=B2_APPLICATION_KEY,
            config=BotoConfig(signature_version="s3v4"),
        )
    return _b2_client


def b2_configured():
    return b2_client() is not None


def allowed_upload(filename):
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    return ext in ALLOWED_UPLOAD_EXTENSIONS


FILE_CATEGORY_PREFIXES = {
    "student-photo": "students/{entityId}/photo/",
    "student-document": "students/{entityId}/documents/",
    "teacher-document": "teachers/{entityId}/documents/",
    "staff-document": "staff/{entityId}/documents/",
    "fee-receipt": "fees/{entityId}/receipts/",
    "report": "reports/{entityId}/",
    "school-document": "school-documents/",
    "backup": "backups/",
}


@app.route("/api/files/upload", methods=["POST"])
@login_required
@csrf_protect
def upload_file():
    u = current_user()
    if not b2_configured():
        return json_error("File storage is not configured on this server.", 503)

    category = request.form.get("category")
    entity_id = request.form.get("entityId", "")
    if category not in FILE_CATEGORY_PREFIXES:
        return json_error("Unknown file category.", 400)

    if "file" not in request.files:
        return json_error("No file provided.", 400)
    file = request.files["file"]
    if not file.filename:
        return json_error("No file selected.", 400)
    if not allowed_upload(file.filename):
        return json_error("File type not allowed.", 400)

    safe_name = secure_filename(file.filename)
    prefix = FILE_CATEGORY_PREFIXES[category].format(entityId=entity_id or "misc")
    key = f"{prefix}{int(time.time())}_{safe_name}"
    content_type = mimetypes.guess_type(safe_name)[0] or "application/octet-stream"

    try:
        b2_client().put_object(
            Bucket=B2_BUCKET_NAME, Key=key, Body=file.stream.read(),
            ContentType=content_type,
        )
    except (ClientError, BotoCoreError) as exc:
        safe_log_error("B2 upload failed", exc)
        return json_error("File upload failed.", 502)

    file_id = new_id("file")
    data = {
        "id": file_id, "key": key, "originalName": file.filename,
        "contentType": content_type, "category": category, "entityId": entity_id,
        "uploadedBy": u.username, "uploadedAt": now().isoformat(),
    }
    rec = create_record("files", data, record_id=file_id, user=u)
    log_activity(f"{u.name} uploaded a file ({category}: {file.filename})", u)
    return jsonify(rec.to_dict()), 201


@app.route("/api/files/<file_id>", methods=["GET"])
@login_required
def get_file_url(file_id):
    if not b2_configured():
        return json_error("File storage is not configured on this server.", 503)
    rec = db().query(Record).filter_by(store="files", id=file_id, deleted=False).first()
    if not rec:
        return json_error("Not found.", 404)

    # Authorization: staff with the "documents" module may access any file;
    # a Student/Parent account may only access files tied to their own record.
    u = current_user()
    if not role_has_module(u.role, "documents") and not role_has_module(u.role, "hr"):
        if u.role in STUDENT_PORTAL_ROLES:
            if rec.data.get("entityId") not in student_ids_for_portal_user(u):
                return json_error("Not found.", 404)
        else:
            return json_error("You do not have access to files.", 403)

    try:
        url = b2_client().generate_presigned_url(
            "get_object",
            Params={"Bucket": B2_BUCKET_NAME, "Key": rec.data["key"]},
            ExpiresIn=300,
        )
    except (ClientError, BotoCoreError) as exc:
        safe_log_error("B2 presign failed", exc)
        return json_error("Could not generate file access link.", 502)

    return jsonify({"url": url, "expiresIn": 300, "meta": rec.to_dict()})


@app.route("/api/files/<file_id>", methods=["DELETE"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def delete_file(file_id):
    u = current_user()
    rec = db().query(Record).filter_by(store="files", id=file_id, deleted=False).first()
    if not rec:
        return json_error("Not found.", 404)
    if b2_configured():
        try:
            b2_client().delete_object(Bucket=B2_BUCKET_NAME, Key=rec.data["key"])
        except (ClientError, BotoCoreError) as exc:
            safe_log_error("B2 delete failed", exc)
    soft_delete_record("files", file_id, user=u)
    return jsonify({"ok": True})


# ==============================================================================
# 17. BACKUPS — full DB export, gzip, upload to B2 under backups/, keep all
#     versions (bucket lifecycle already configured to keep all versions;
#     we additionally use timestamped keys so nothing is ever overwritten).
# ==============================================================================

def export_full_database():
    s = db()
    dump = {"exportedAt": now().isoformat(), "stores": {}}
    for store in STORES:
        rows = s.query(Record).filter_by(store=store, deleted=False).all()
        dump["stores"][store] = [r.to_dict() for r in rows]
    users = s.query(User).filter_by(deleted=False).all()
    dump["users"] = [
        {**u.to_public_dict(), "passwordHash": u.password_hash} for u in users
    ]
    return dump


@app.route("/api/backups", methods=["POST"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def create_backup():
    if not b2_configured():
        return json_error("File storage is not configured on this server.", 503)
    u = current_user()
    dump = export_full_database()
    raw = json.dumps(dump, default=str).encode("utf-8")
    compressed = gzip.compress(raw)
    ts = now().strftime("%Y%m%d-%H%M%S")
    key = f"backups/backup-{ts}-{secrets.token_hex(3)}.json.gz"
    try:
        b2_client().put_object(
            Bucket=B2_BUCKET_NAME, Key=key, Body=compressed,
            ContentType="application/gzip",
        )
    except (ClientError, BotoCoreError) as exc:
        safe_log_error("B2 backup upload failed", exc)
        return json_error("Backup upload failed.", 502)
    log_activity(f"{u.name} created a full database backup ({key})", u)
    return jsonify({"key": key, "sizeBytes": len(compressed), "createdAt": now().isoformat()}), 201


@app.route("/api/backups", methods=["GET"])
@login_required
@require_roles(*PRIVILEGED_STAFF_ROLES)
def list_backups():
    if not b2_configured():
        return json_error("File storage is not configured on this server.", 503)
    try:
        resp = b2_client().list_objects_v2(Bucket=B2_BUCKET_NAME, Prefix="backups/")
    except (ClientError, BotoCoreError) as exc:
        safe_log_error("B2 list backups failed", exc)
        return json_error("Could not list backups.", 502)
    items = [
        {"key": o["Key"], "sizeBytes": o["Size"], "lastModified": o["LastModified"].isoformat()}
        for o in resp.get("Contents", [])
    ]
    items.sort(key=lambda x: x["lastModified"], reverse=True)
    return jsonify({"items": items, "total": len(items)})


# ==============================================================================
# 18. SYNC — server-authoritative delta sync for the offline IndexedDB layer
# ==============================================================================

@app.route("/api/sync", methods=["GET"])
@login_required
def sync_pull():
    u = current_user()
    since = parse_iso(request.args.get("since"))
    only_store = request.args.get("store")
    s = db()
    result = {}
    stores = [only_store] if only_store else STORES
    for store in stores:
        q = s.query(Record).filter_by(store=store)
        if since:
            q = q.filter(Record.updated_at > since)
        rows = q.order_by(Record.updated_at.asc()).limit(5000).all()
        scope = build_scope_filter(store, u)
        if scope:
            rows = [r for r in rows if r.deleted or scope(r)]
        result[store] = [r.to_dict() for r in rows]
    return jsonify({"serverTime": now().isoformat(), "stores": result})


@app.route("/api/sync", methods=["POST"])
@login_required
@csrf_protect
def sync_push():
    u = current_user()
    payload = request.get_json(silent=True) or {}
    changes = payload.get("changes") or []
    s = db()
    applied, conflicts = [], []

    for change in changes:
        store = change.get("store")
        rid = change.get("id")
        base_rev = change.get("baseRev")
        data = change.get("data") or {}
        if store not in STORES or not rid:
            continue
        module = STORE_MODULE.get(store, store)
        if not role_has_module(u.role, module):
            conflicts.append({"store": store, "id": rid, "reason": "forbidden"})
            continue

        existing = s.query(Record).filter_by(store=store, id=rid).first()
        if existing and base_rev is not None and existing.rev != base_rev:
            # Server is authoritative: reject client write, hand back server state.
            conflicts.append({"store": store, "id": rid, "reason": "revision_conflict",
                               "server": existing.to_dict()})
            continue

        if change.get("deleted"):
            if existing:
                existing.deleted = True
                existing.rev = (existing.rev or 1) + 1
                existing.updated_by = u.username
            applied.append({"store": store, "id": rid, "action": "deleted"})
            continue

        data["id"] = rid
        if existing:
            existing.data = data
            existing.deleted = False
            existing.rev = (existing.rev or 1) + 1
            existing.updated_by = u.username
            for k, v in _extract_indexed_fields(data).items():
                setattr(existing, k, v)
        else:
            rec = Record(id=rid, store=store, data=data, rev=1,
                         updated_by=u.username, **_extract_indexed_fields(data))
            s.add(rec)
        applied.append({"store": store, "id": rid, "action": "upserted"})

    s.commit()
    return jsonify({"applied": applied, "conflicts": conflicts, "serverTime": now().isoformat()})


# ==============================================================================
# 19. INDEXEDDB MIGRATION — one-time (or repeatable) import of a browser's
#     existing IndexedDB dump. Never deletes IndexedDB itself (that's a
#     frontend decision); never overwrites existing server records that
#     differ from the incoming ones — those are reported as conflicts for a
#     human to resolve.
# ==============================================================================

@app.route("/api/migrate", methods=["POST"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def migrate_indexeddb():
    u = current_user()
    payload = request.get_json(silent=True) or {}
    dump = payload.get("dump") or {}
    s = db()

    summary = {}
    for store, records in dump.items():
        if store == "users":
            summary["users"] = _migrate_users(records, u)
            continue
        if store not in STORES:
            continue
        inserted, duplicates, conflicts = 0, 0, []
        for rec in records or []:
            rid = rec.get("id")
            if not rid:
                continue
            existing = s.query(Record).filter_by(store=store, id=rid).first()
            if not existing:
                clean = {k: v for k, v in rec.items() if not k.startswith("_")}
                new_rec = Record(id=rid, store=store, data=clean, rev=1,
                                 updated_by=u.username, **_extract_indexed_fields(clean))
                s.add(new_rec)
                inserted += 1
            else:
                existing_clean = {k: v for k, v in (existing.data or {}).items() if not k.startswith("_")}
                incoming_clean = {k: v for k, v in rec.items() if not k.startswith("_")}
                if existing_clean == incoming_clean:
                    duplicates += 1
                else:
                    conflicts.append({"id": rid, "server": existing_clean, "incoming": incoming_clean})
        summary[store] = {"inserted": inserted, "duplicates": duplicates, "conflicts": conflicts}
    s.commit()
    log_activity(f"{u.name} ran an IndexedDB migration import", u)
    return jsonify(summary)


def _migrate_users(records, admin_user):
    """Migrating legacy 'users' records is special-cased: the client's
    legacy password hash is NOT compatible with our server-side hashing
    scheme, so we never import it as a usable credential. New accounts are
    created with a random 4-digit PIN and mustChange=True so an
    administrator (or the user, on next login) resets it properly; accounts
    that already exist on the server (by username) are left untouched."""
    s = db()
    inserted, skipped, conflicts = 0, 0, []
    for rec in records or []:
        username = (rec.get("username") or "").strip()
        if not username:
            continue
        if s.query(User).filter(func.lower(User.username) == username.lower()).first():
            skipped += 1
            continue
        role = rec.get("role") if rec.get("role") in ROLE_MODULES else "Student"
        temp_pin = str(random.randint(1000, 9999))
        user = User(
            id=rec.get("id") or new_id("u"), username=username,
            password_hash=generate_password_hash(temp_pin),
            name=rec.get("name", username), role=role, active=bool(rec.get("active", True)),
            linked_teacher_id=rec.get("linkedTeacherId"),
            linked_student_ids=rec.get("linkedStudentIds") or [],
            must_change=True,
        )
        s.add(user)
        inserted += 1
    return {"inserted": inserted, "skippedExisting": skipped, "note":
            "Imported accounts were given a new random PIN and require a password change; "
            "legacy client-side password hashes cannot be reused for server authentication."}


# ==============================================================================
# 20. HEALTH CHECK
# ==============================================================================

@app.route("/api/health", methods=["GET"])
def health():
    status = {"status": "ok", "time": now().isoformat()}
    try:
        db().execute("SELECT 1")
        status["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        safe_log_error("health check DB failure", exc)
        status["database"] = "error"
        status["status"] = "degraded"
    status["fileStorage"] = "configured" if b2_configured() else "not_configured"
    code = 200 if status["status"] == "ok" else 503
    return jsonify(status), code


# ==============================================================================
# 21. SERVE THE EXISTING FRONTEND (index.html + any sibling static assets)
# ==============================================================================

@app.route("/", methods=["GET"])
def serve_index():
    return send_from_directory(FRONTEND_DIR, FRONTEND_INDEX)


@app.route("/<path:filename>", methods=["GET"])
def serve_static_asset(filename):
    # Never allow this catch-all to serve app.py, .env, or the database file.
    forbidden = {"app.py", ".env", ".env.example", "requirements.txt"}
    if filename in forbidden or filename.endswith(".db") or filename.startswith("."):
        abort(404)
    full_path = os.path.join(FRONTEND_DIR, filename)
    if os.path.isfile(full_path):
        return send_from_directory(FRONTEND_DIR, filename)
    abort(404)


# ==============================================================================
# 22. SECURITY HEADERS + ERROR HANDLERS
# ==============================================================================

@app.after_request
def set_security_headers(resp):
    resp.headers["X-Content-Type-Options"] = "nosniff"
    resp.headers["X-Frame-Options"] = "DENY"
    resp.headers["Referrer-Policy"] = "same-origin"
    if CORS_ALLOWED_ORIGIN:
        resp.headers["Access-Control-Allow-Origin"] = CORS_ALLOWED_ORIGIN
        resp.headers["Access-Control-Allow-Credentials"] = "true"
        resp.headers["Access-Control-Allow-Headers"] = "Content-Type, X-CSRF-Token"
        resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
    return resp


@app.errorhandler(404)
def not_found(_e):
    if request.path.startswith("/api/"):
        return json_error("Not found.", 404)
    return send_from_directory(FRONTEND_DIR, FRONTEND_INDEX)


@app.errorhandler(413)
def too_large(_e):
    return json_error(f"File too large (max {MAX_UPLOAD_MB} MB).", 413)


@app.errorhandler(500)
def server_error(exc):
    safe_log_error("Unhandled server error", exc)
    return json_error("An unexpected error occurred.", 500)


# ==============================================================================
# 23. SEED DATA — first run only (mirrors seedIfEmpty() in the frontend)
# ==============================================================================

DEFAULT_GRADING_SCALE = [
    {"grade": "A", "min": 80, "max": 100, "points": 12, "remark": "Excellent"},
    {"grade": "A-", "min": 75, "max": 79, "points": 11, "remark": "Very Good"},
    {"grade": "B+", "min": 70, "max": 74, "points": 10, "remark": "Good"},
    {"grade": "B", "min": 65, "max": 69, "points": 9, "remark": "Good"},
    {"grade": "B-", "min": 60, "max": 64, "points": 8, "remark": "Above Average"},
    {"grade": "C+", "min": 55, "max": 59, "points": 7, "remark": "Average"},
    {"grade": "C", "min": 50, "max": 54, "points": 6, "remark": "Average"},
    {"grade": "C-", "min": 45, "max": 49, "points": 5, "remark": "Below Average"},
    {"grade": "D+", "min": 40, "max": 44, "points": 4, "remark": "Weak"},
    {"grade": "D", "min": 35, "max": 39, "points": 3, "remark": "Weak"},
    {"grade": "D-", "min": 30, "max": 34, "points": 2, "remark": "Poor"},
    {"grade": "E", "min": 0, "max": 29, "points": 1, "remark": "Very Poor"},
]
DEFAULT_CBC_SCALE = [
    {"grade": "EE", "min": 75, "max": 100, "points": 7, "remark": "Exceeding Expectation"},
    {"grade": "ME2", "min": 65, "max": 74, "points": 6, "remark": "Meeting Expectation (Upper)"},
    {"grade": "ME1", "min": 50, "max": 64, "points": 5, "remark": "Meeting Expectation"},
    {"grade": "AE2", "min": 40, "max": 49, "points": 4, "remark": "Approaching Expectation (Upper)"},
    {"grade": "AE1", "min": 25, "max": 39, "points": 3, "remark": "Approaching Expectation"},
    {"grade": "BE2", "min": 15, "max": 24, "points": 2, "remark": "Below Expectation (Upper)"},
    {"grade": "BE1", "min": 0, "max": 14, "points": 1, "remark": "Below Expectation"},
]


def seed_if_empty():
    s = db()
    if not s.query(User).first():
        admin_password = env("ADMIN_INITIAL_PASSWORD", "admin123")
        admin = User(
            id=new_id("u"), username="admin",
            password_hash=generate_password_hash(admin_password),
            name="System Administrator", role="Administrator", active=True,
            must_change=True,
        )
        s.add(admin)
        log.info("Seeded default admin account (username=admin). "
                 "Set ADMIN_INITIAL_PASSWORD in .env before first deploy, "
                 "and change the password on first login.")

    if not s.query(Record).filter_by(store="grading").first():
        for g in DEFAULT_GRADING_SCALE:
            rid = new_id("gr")
            data = {**g, "id": rid}
            s.add(Record(id=rid, store="grading", data=data, rev=1))

    if not s.query(Record).filter_by(store="cbcGrading").first():
        for g in DEFAULT_CBC_SCALE:
            rid = new_id("cbc")
            data = {**g, "id": rid}
            s.add(Record(id=rid, store="cbcGrading", data=data, rev=1))

    if not s.query(Record).filter_by(store="settings", id="school").first():
        data = {
            "id": "school", "schoolName": "ARNESEN'S COMPREHENSIVE SCHOOL",
            "motto": "Discipline and Hardwork for success",
            "address": "P.O. Box 036\u201330102, Burnt Forest, Kenya",
            "phone": "+254 700 000 000", "email": "info@arnesenscomprehensive.sc.ke",
            "currentYear": str(date.today().year), "currentTerm": "Term 1",
            "autoLogoutMin": 20, "logoDataUrl": "", "gradingSystem": "844",
        }
        s.add(Record(id="school", store="settings", data=data, rev=1))

    s.commit()


with app.app_context():
    seed_if_empty()
    if not B2_KEY_ID or not B2_APPLICATION_KEY:
        log.warning("B2 credentials are not fully configured — file uploads, "
                     "backups and private file access will return 503 until "
                     "B2_KEY_ID / B2_APPLICATION_KEY / B2_BUCKET_NAME / "
                     "B2_ENDPOINT are set in the environment.")


# ==============================================================================
# 24. ENTRYPOINT
# ==============================================================================

if __name__ == "__main__":
    port = int(env("PORT", "5000"))
    debug = env("FLASK_DEBUG", "0") == "1"
    app.run(host="0.0.0.0", port=port, debug=debug)
