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
import atexit
import hashlib
import threading
import urllib.request
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, date
from functools import wraps

from flask import (
    Flask, request, jsonify, session, send_from_directory, g, abort, Response
)
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from sqlalchemy import (
    create_engine, Column, String, Integer, Boolean, DateTime, Text, JSON,
    Index, or_, and_, func, text
)
from sqlalchemy import event
from sqlalchemy.orm import sessionmaker, scoped_session, declarative_base, Session
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
    if isinstance(val, str):
        # Pasted values often carry stray spaces/newlines or wrapping quotes,
        # which break B2's request signature (SignatureDoesNotMatch).
        val = val.strip().strip('"').strip("'").strip()
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

if DATABASE_URL.startswith("sqlite"):
    # Every save used to fsync twice (the record + its activity-log row). WAL + NORMAL keeps the
    # data safe against crashes but makes each commit several times faster, and lets reads
    # continue while a write is happening.
    @event.listens_for(engine, "connect")
    def _sqlite_fast_pragmas(dbapi_conn, _record):
        cur = dbapi_conn.cursor()
        try:
            cur.execute("PRAGMA journal_mode=WAL")
            cur.execute("PRAGMA synchronous=NORMAL")
            cur.execute("PRAGMA temp_store=MEMORY")
            cur.execute("PRAGMA cache_size=-20000")
            cur.execute("PRAGMA busy_timeout=5000")
        finally:
            cur.close()
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
    "formative", "examResources", "smsLog",
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


class B2Index(Base):
    """What this server last wrote to / saw in Backblaze, one row per B2 object.
    Lets the server tell "deleted here" from "deleted in B2"."""
    __tablename__ = "b2_index"

    key = Column(String(500), primary_key=True)
    store = Column(String(40), nullable=False)
    members = Column(JSON, default=list)   # record ids stored in that B2 object
    hash = Column(String(64), nullable=False)
    etag = Column(String(80), nullable=True)


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
                       "promotions", "marks", "analysis", "reportcards", "attendance", "fees", "timetable",
                       "library", "inventory", "hr", "communication", "users", "settings",
                       "logs", "leaveouts", "documents", "notes", "lists", "sms", "formative",
                       "combined", "examresources", "links", "configurations"],
    "Head Teacher": ["dashboard", "exams", "promotions", "marks", "analysis", "reportcards", "attendance",
                      "fees", "communication", "leaveouts", "documents", "notes", "lists", "sms",
                      "formative", "combined", "examresources", "links"],
    "Deputy Head Teacher": ["dashboard", "attendance", "exams", "analysis", "discipline",
                             "communication", "leaveouts", "documents", "notes", "lists",
                             "combined", "examresources", "links"],
    "Registrar": ["dashboard", "students", "communication", "leaveouts", "documents", "lists", "links"],
    "Class Teacher": ["dashboard", "marks", "promotions", "analysis", "reportcards", "attendance",
                       "communication", "leaveouts", "documents", "notes", "lists", "formative",
                       "combined", "examresources", "links"],
    "Subject Teacher": ["dashboard", "marks", "analysis", "communication", "notes", "formative",
                         "examresources", "links"],
    "Bursar": ["dashboard", "fees", "inventory", "communication", "documents", "lists", "sms", "links"],
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
    "formative": "formative", "examResources": "examresources", "smsLog": "sms",
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
TEACHER_SCOPED_STORES = {"students", "marks", "attendance", "teacherNotes", "discipline", "formative"}


def get_grading_permissions_map():
    """settings store may hold an id='permissions' record overriding ROLE_MODULES."""
    rec = db().query(Record).filter_by(store="settings", id="permissions", deleted=False).first()
    if rec and isinstance(rec.data, dict):
        return rec.data
    return None


# Reference data that staff need to *read* (to pick a pupil, show a class name, build a
# list) even when their role does not own the module that manages it. Writes still
# require the owning module; teacher scoping still applies to what is returned.
REFERENCE_READ = {
    "students": {"fees", "discipline", "lists", "sms", "library", "leaveouts", "formative",
                 "combined", "reportcards"},
    "teachers": {"hr", "sms", "lists", "timetable"},
}
STAFF_REFERENCE_STORES = {"classes", "subjects"}


def ref_read_ok(u, store):
    if u.role in STUDENT_PORTAL_ROLES:
        return False
    if store in STAFF_REFERENCE_STORES:
        return True
    mods = REFERENCE_READ.get(store)
    return bool(mods) and any(role_has_module(u.role, m) for m in mods)


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
    # The Class Teacher of a class may work with every subject inside that class.
    if class_id:
        cdata = _record_data("classes", class_id) or {}
        if cdata.get("classTeacherId") and cdata.get("classTeacherId") == user.linked_teacher_id:
            return True
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
        # No perPage given means "give me everything". (This used to be max(1, ...), which silently
        # turned "no page size" into a page size of 1, so every list call returned a single record
        # and the full data only appeared when the 25-second background sync caught up.)
        per_page = int(args.get("perPage", args.get("per_page", 0)) or 0)
        per_page = min(500, per_page) if per_page > 0 else 0
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
            own_sections = {_class_section(c) for c in own_classes if c}
            return lambda r: ((r.data or {}).get("classId") in own_classes
                              or ((r.data or {}).get("section") and not (r.data or {}).get("classId")
                                  and (r.data or {}).get("section") in own_sections))

    if user.role in TEACHER_ROLES and store in TEACHER_SCOPED_STORES and user.role not in PRIVILEGED_STAFF_ROLES:
        visible_classes = teacher_visible_class_ids(user)
        if store == "students":
            return lambda r: (r.data or {}).get("classId") in visible_classes
        if store in ("marks", "attendance", "formative"):
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
    "examResources": "exam-resources",
    "smsLog": "sms-log",
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
        if store not in PUBLIC_READ_STORES and not role_has_module(u.role, module) and not ref_read_ok(u, store):
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
        if store == "activity":
            # Any signed-in user's actions may be appended to the activity log
            # (text/user/time only). Existing entries can never be edited/deleted.
            payload = request.get_json(silent=True) or {}
            pid = payload.get("id")
            if pid and db().query(Record).filter_by(store="activity", id=pid).first():
                pid = None  # never overwrite an existing log entry
            entry = {"id": pid, "text": str(payload.get("text", ""))[:500],
                     "user": u.name, "time": now().isoformat()}
            rec = create_record("activity", entry, user=u)
            return jsonify(rec.to_dict()), 201
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
        if store == "activity":
            if db().query(Record).filter_by(store="activity", id=record_id).first():
                return json_error("Activity log entries cannot be edited.", 403)
            return json_error("Not found.", 404)  # client then creates it via POST
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
        has_scope = bool(payload.get("section")) or bool(payload.get("classId"))
        if payload.get("section") and payload.get("section") not in ("Primary", "JSS"):
            return "Section must be Primary or JSS."
        if not has_scope or not payload.get("year") or not to_float(payload.get("amount")):
            return "Section, academic year and a valid amount are required."
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


USERNAME_RE = re.compile(r"^[A-Za-z0-9._-]{3,30}$")
USERNAME_RULE_MSG = "Username must be 3-30 characters: letters, numbers, dot, dash or underscore."


@app.route("/api/users/check-username", methods=["GET"])
@login_required
@require_roles("Administrator")
def check_username():
    username = (request.args.get("username") or "").strip()
    exclude_id = request.args.get("excludeId")
    if not USERNAME_RE.match(username):
        return jsonify({"available": False, "valid": False, "reason": USERNAME_RULE_MSG})
    s = db()
    q = s.query(User).filter(func.lower(User.username) == username.lower())
    if exclude_id:
        q = q.filter(User.id != exclude_id)
    return jsonify({"available": q.first() is None, "valid": True})


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
    if not USERNAME_RE.match(username):
        return json_error(USERNAME_RULE_MSG, 400)
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
    if isinstance(payload.get("username"), str):
        new_un = payload["username"].strip()
        if new_un and new_un != user.username:
            if user.username == "admin":
                return json_error("The primary admin username cannot be changed.", 400)
            if not USERNAME_RE.match(new_un):
                return json_error(USERNAME_RULE_MSG, 400)
            clash = s.query(User).filter(func.lower(User.username) == new_un.lower(), User.id != user.id).first()
            if clash:
                return json_error("Username already exists.", 409)
            user.username = new_un
    if "linkedTeacherId" in payload:
        user.linked_teacher_id = payload["linkedTeacherId"] or None
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


PROMOTION_EXAM_TYPES = {"End-Year Exam", "Annual Exam"}


@app.route("/api/promotions/apply", methods=["POST"])
@login_required
@csrf_protect
def promotions_apply():
    """End-of-year promotion. Administrator / Head Teacher: any class. Class Teacher: only
    pupils of the class they are class teacher of. Promotion is only allowed from an
    end-of-year exam, is recorded in the pupil's promotionHistory (so it cannot be applied
    twice for the same exam) and goes through the normal revision/updated_by bookkeeping."""
    u = current_user()
    if not role_has_module(u.role, "promotions"):
        return json_error("You do not have access to class promotion.", 403)
    payload = request.get_json(silent=True) or {}
    exam_id = payload.get("examId")
    moves = payload.get("moves") or []
    exam = _record_data("exams", exam_id) if exam_id else None
    if not exam:
        return json_error("Examination not found.", 404)
    if exam.get("type") not in PROMOTION_EXAM_TYPES:
        return json_error("Pupils can only be promoted from an end-of-year examination.", 400)
    if not isinstance(moves, list) or not moves or len(moves) > 2000:
        return json_error("No pupils to promote.", 400)
    full_access = u.role in ("Administrator", "Head Teacher")
    s = db()
    counts = {"promoted": 0, "stayed": 0, "completed": 0, "skipped": 0}
    out = []
    try:
        for mv in moves:
            action = mv.get("action")
            if action not in ("promote", "stay", "graduate"):
                s.rollback()
                return json_error("Invalid promotion action.", 400)
            rec = s.query(Record).filter_by(store="students", id=mv.get("studentId"), deleted=False).first()
            if not rec:
                continue
            data = dict(rec.data or {})
            from_class = data.get("classId")
            if not full_access:
                cdata = _record_data("classes", from_class) or {}
                if not (u.linked_teacher_id and cdata.get("classTeacherId") == u.linked_teacher_id):
                    s.rollback()
                    return json_error("You can only promote pupils of the class you are class teacher of.", 403)
            hist = list(data.get("promotionHistory") or [])
            if any(h.get("examId") == exam_id for h in hist):
                counts["skipped"] += 1
                continue
            to_class = None
            if action == "promote":
                to_class = mv.get("toClassId")
                if not to_class or not _record_data("classes", to_class):
                    s.rollback()
                    return json_error("Choose a valid class to promote into.", 400)
                data["classId"] = to_class
                counts["promoted"] += 1
            elif action == "graduate":
                data["status"] = "Archived"
                counts["completed"] += 1
            else:
                counts["stayed"] += 1
            hist.append({"examId": exam_id, "year": exam.get("year"), "action": action,
                         "fromClassId": from_class, "toClassId": to_class,
                         "date": datetime.utcnow().isoformat(timespec="seconds"), "by": u.username})
            data["promotionHistory"] = hist
            rec.data = data
            rec.rev = (rec.rev or 1) + 1
            rec.updated_by = u.username
            for k, v in _extract_indexed_fields(data).items():
                setattr(rec, k, v)
            out.append(data)
        s.commit()
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("promotions_apply failed", exc)
        return json_error("Promotion failed; no changes were applied.", 500)
    log_activity(f"{u.name} ran class promotion for exam {exam.get('name')}: {counts['promoted']} promoted, "
                 f"{counts['stayed']} stayed, {counts['completed']} completed", u)
    return jsonify({**counts, "students": out})


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

_PRIMARY_LEVELS = ("PP1", "PP2", "Grade 1", "Grade 2", "Grade 3", "Grade 4", "Grade 5", "Grade 6")


def _class_section(class_id):
    """'Primary' for PP1-Grade 6 classes, otherwise 'JSS' (mirrors classDivision() in the frontend)."""
    cls = _record_data("classes", class_id) if class_id else None
    return "Primary" if cls and cls.get("level") in _PRIMARY_LEVELS else "JSS"


def fee_balance_for(student_id, year):
    s = db()
    student = _record_data("students", student_id)
    if not student:
        return None
    class_id = student.get("classId")
    # Fees are per SECTION (Primary / JSS); a class-specific record overrides its section's for that category.
    section = _class_section(class_id)
    all_structs = s.query(Record).filter_by(store="feeStructures", deleted=False, year=str(year)).all()
    by_cat = {}
    for r in all_structs:
        d = r.data or {}
        if d.get("term"):
            continue
        if d.get("section") and not d.get("classId") and d.get("section") == section:
            by_cat[(d.get("category") or "Tuition")] = d
    for r in all_structs:
        d = r.data or {}
        if not d.get("term") and d.get("classId") and d.get("classId") == class_id:
            by_cat[(d.get("category") or "Tuition")] = d
    billed = sum(to_float(d.get("amount")) for d in by_cat.values())
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


# ------------------------------------------------------------------------------
# SMS gateway (Africa's Talking). Configure with environment variables:
#   AT_USERNAME, AT_API_KEY, optional AT_SENDER_ID, and AT_SANDBOX=1 for testing.
# Without them the route answers 503 and the UI falls back to tap-to-send links.
# ------------------------------------------------------------------------------
def normalize_ke_phone(raw):
    d = re.sub(r"\D", "", str(raw or ""))
    if len(d) == 12 and d.startswith("254"):
        return "+" + d
    if len(d) == 10 and d.startswith("0"):
        return "+254" + d[1:]
    if len(d) == 9 and d[0] in "17":
        return "+254" + d
    return None


@app.route("/api/sms/send", methods=["POST"])
@login_required
@csrf_protect
@require_module("sms")
def sms_send():
    at_user, at_key = env("AT_USERNAME"), env("AT_API_KEY")
    if not at_user or not at_key:
        return json_error("SMS gateway is not configured on this server.", 503, configured=False)
    payload = request.get_json(silent=True) or {}
    msgs = payload.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return json_error("No messages supplied.", 400)
    if len(msgs) > 500:
        return json_error("Send at most 500 messages per request.", 400)
    url = ("https://api.sandbox.africastalking.com/version1/messaging"
           if env("AT_SANDBOX", "0") == "1" else "https://api.africastalking.com/version1/messaging")
    sender = env("AT_SENDER_ID")

    def _send_one(m):
        to = normalize_ke_phone((m or {}).get("to"))
        text = str((m or {}).get("message") or "")[:918]
        if not to or not text.strip():
            return False
        form = {"username": at_user, "to": to, "message": text}
        if sender:
            form["from"] = sender
        req = urllib.request.Request(
            url, data=urllib.parse.urlencode(form).encode(),
            headers={"apiKey": at_key, "Accept": "application/json",
                     "Content-Type": "application/x-www-form-urlencoded"})
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                body = json.loads(resp.read().decode() or "{}")
            recips = (body.get("SMSMessageData") or {}).get("Recipients") or []
            return bool(recips) and all(str(x.get("statusCode")) in ("100", "101", "102") for x in recips)
        except Exception as exc:
            safe_log_error("SMS send failed", exc)
            return False

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(_send_one, msgs))
    sent = sum(1 for ok in results if ok)
    u = current_user()
    log_activity(f"{u.name} sent {sent} SMS ({len(results) - sent} failed)", u)
    return jsonify({"sent": sent, "failed": len(results) - sent})


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
    if not any(role_has_module(u.role, m) for m in ("documents", "hr", "examresources")):
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


@app.route("/api/cache/clear-activity", methods=["POST"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def clear_activity_log():
    """Administrator-only. Soft-deletes ONLY records in the 'activity' store (the audit/history log)
    in one request. No other store (students, marks, fees, timetable, ...) is ever touched."""
    s = db()
    n = s.query(Record).filter_by(store="activity", deleted=False).update(
        {"deleted": True}, synchronize_session=False)
    s.commit()
    log_activity(f"{current_user().name} cleared the activity log ({n} entries)", current_user())
    return jsonify({"ok": True, "cleared": n})


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
# 17b. BACKBLAZE B2 AS THE DURABLE HOME OF ALL DATA  (two-way, item by item)
# ------------------------------------------------------------------------------
# Every kind of data gets its own folder in the bucket, and every item is its
# own file:
#
#   data/pupils/0123_Jane_Doe__<id>.json     data/teachers/...    data/classes/...
#   data/subjects/...  data/exams/...  data/fee-payments/...  data/users/...
#   (marks are grouped per exam, attendance per day, activity log per day)
#
#   system -> B2 : add / change / DELETE an item here and its file is created,
#                  updated or deleted in the bucket within ~B2_SYNC_DEBOUNCE_SECONDS.
#   B2 -> system : delete or edit a file in the bucket and the system follows
#                  within ~B2_POLL_SECONDS. A brand-new server with an empty
#                  database rebuilds itself from the bucket.
#
# A small table (b2_index) remembers what was last synced so the server can
# tell "deleted here" apart from "deleted in B2". Set B2_AUTOSYNC=0 to turn
# all of this off.
# ==============================================================================

B2_AUTOSYNC = env("B2_AUTOSYNC", "1") == "1"
B2_SYNC_DEBOUNCE_SECONDS = max(2, int(env("B2_SYNC_DEBOUNCE_SECONDS", "10")))
B2_POLL_SECONDS = max(15, int(env("B2_POLL_SECONDS", "60")))
FILE_RECONCILE_EVERY_N_POLLS = 10
DATA_PREFIX = "data/"
LEGACY_SNAPSHOT_KEY = "data/latest.json.gz"   # old single-file format (read once, then unused)

FOLDER_FOR_STORE = {
    "students": "pupils", "teachers": "teachers", "classes": "classes", "subjects": "subjects",
    "exams": "exams", "marks": "marks", "attendance": "attendance",
    "feeStructures": "fee-structures", "feePayments": "fee-payments",
    "grading": "grading", "cbcGrading": "cbc-grading", "announcements": "announcements",
    "books": "library-books", "borrows": "library-borrows", "timetable": "timetable",
    "timetableRules": "timetable-rules", "settings": "settings", "activity": "activity-log",
    "discipline": "discipline", "inventoryItems": "inventory-items", "suppliers": "suppliers",
    "stockMovements": "stock-movements", "assets": "assets", "leaveRequests": "leave-requests",
    "payrollRecords": "payroll", "appraisals": "appraisals", "staffDocuments": "staff-documents",
    "leaveOuts": "leave-outs", "files": "files", "teacherNotes": "class-notes",
    "users": "users",
}
for _st in STORES:
    FOLDER_FOR_STORE.setdefault(_st, _st)
STORE_FOR_FOLDER = {v: k for k, v in FOLDER_FOR_STORE.items()}

_sync_lock = threading.RLock()
_tls = threading.local()          # "quiet" = this thread's commits must not trigger an upload
_sync_timer = None
_poll_started = False
_sync_state = {"lastSuccessAt": None, "lastPullAt": None, "lastError": None,
               "blocked": False, "restoredFromB2": False, "needsPush": True}


def _safe_part(text, limit=60):
    t = re.sub(r"[^A-Za-z0-9._-]+", "-", str(text or "")).strip("-.")
    return t[:limit]


def _norm_etag(e):
    return str(e or "").strip('"')


def _readable_name(store, d):
    if store == "students":
        parts = [d.get("admissionNo"), d.get("firstName"), d.get("lastName")]
    elif store == "teachers":
        parts = [d.get("firstName"), d.get("lastName")]
    elif store == "classes":
        parts = [d.get("level") or d.get("name"), d.get("stream")]
    elif store in ("subjects", "exams"):
        parts = [d.get("name")]
    else:
        return ""
    return _safe_part("_".join(str(p) for p in parts if p))


def _chunk_id(store, d, rec_date):
    """High-volume stores are grouped so one exam / one day is one file."""
    if store == "marks":
        return d.get("examId") or "no-exam"
    if store == "attendance":
        return d.get("date") or rec_date or "no-date"
    if store == "activity":
        return str(d.get("time") or "")[:10] or "undated"
    return None


def _payload_hash(payload):
    p = payload
    if "records" in p:
        p = {**p, "records": sorted(p["records"], key=lambda r: str(r.get("id")))}
    return hashlib.sha256(json.dumps(p, sort_keys=True, default=str,
                                     separators=(",", ":")).encode("utf-8")).hexdigest()


def _dump_body(payload):
    p = payload
    if "records" in p:
        p = {**p, "records": sorted(p["records"], key=lambda r: str(r.get("id")))}
    return json.dumps(p, sort_keys=True, default=str, indent=1, ensure_ascii=False).encode("utf-8")


def _user_payload_data(u):
    return {
        "id": u.id, "username": u.username, "passwordHash": u.password_hash,
        "name": u.name, "role": u.role, "active": bool(u.active), "photo": u.photo,
        "linkedTeacherId": u.linked_teacher_id,
        "linkedStudentIds": u.linked_student_ids or [], "mustChange": bool(u.must_change),
    }


def _build_desired(s):
    """Everything that SHOULD be in B2 right now: key -> payload/hash/members."""
    desired, groups = {}, {}
    for r in s.query(Record).filter_by(deleted=False).all():
        if r.store not in FOLDER_FOR_STORE:
            continue
        d = dict(r.data or {})
        d["id"] = r.id
        folder = FOLDER_FOR_STORE[r.store]
        cid = _chunk_id(r.store, d, r.rec_date)
        if cid is None:
            name = _readable_name(r.store, d)
            key = f"{DATA_PREFIX}{folder}/{name + '__' if name else ''}{_safe_part(r.id, 80)}.json"
            desired[key] = {"store": r.store, "members": [r.id],
                            "payload": {"store": r.store, "id": r.id, "data": d}}
        else:
            key = f"{DATA_PREFIX}{folder}/{_safe_part(cid, 80)}.json"
            g = groups.setdefault(key, {"store": r.store, "members": [], "records": []})
            g["members"].append(r.id)
            g["records"].append(d)
    for key, g in groups.items():
        desired[key] = {"store": g["store"], "members": g["members"],
                        "payload": {"store": g["store"], "records": g["records"]}}
    for u in s.query(User).filter_by(deleted=False).all():
        key = f"{DATA_PREFIX}users/{_safe_part(u.username)}__{_safe_part(u.id, 80)}.json"
        desired[key] = {"store": "users", "members": [u.id],
                        "payload": {"store": "users", "id": u.id, "data": _user_payload_data(u)}}
    for v in desired.values():
        v["hash"] = _payload_hash(v["payload"])
    return desired


# ---------------------------------------------------------------- system -> B2

def push_changes_to_b2():
    """Create/update/delete B2 files so the bucket matches the system."""
    if not b2_configured() or _sync_state["blocked"]:
        return False
    with _sync_lock:
        _tls.quiet = True
        s = SessionLocal()
        _sync_state["needsPush"] = False
        try:
            desired = _build_desired(s)
            idx = {r.key: r for r in s.query(B2Index).all()}
            if not desired and idx:
                log.warning("Nothing to sync but B2 index has entries — refusing to delete everything.")
                return False
            to_put = [k for k, v in desired.items() if k not in idx or idx[k].hash != v["hash"]]
            to_del = [k for k in idx if k not in desired]
            client = b2_client()
            failed = False

            def _put(k):
                v = desired[k]
                resp = client.put_object(Bucket=B2_BUCKET_NAME, Key=k, Body=_dump_body(v["payload"]),
                                         ContentType="application/json")
                return k, _norm_etag(resp.get("ETag"))

            def _del(k):
                client.delete_object(Bucket=B2_BUCKET_NAME, Key=k)
                return k

            with ThreadPoolExecutor(max_workers=8) as ex:
                for fut in [ex.submit(_put, k) for k in to_put]:
                    try:
                        k, etag = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        failed = True
                        safe_log_error("B2 upload of one item failed", exc)
                        continue
                    v = desired[k]
                    row = idx.get(k)
                    if row is None:
                        row = B2Index(key=k)
                        s.add(row)
                    row.store, row.members, row.hash, row.etag = v["store"], v["members"], v["hash"], etag
                for fut in [ex.submit(_del, k) for k in to_del]:
                    try:
                        k = fut.result()
                    except Exception as exc:  # noqa: BLE001
                        failed = True
                        safe_log_error("B2 delete of one item failed", exc)
                        continue
                    s.delete(idx[k])
            s.commit()
            _sync_state["needsPush"] = failed
            if not failed:
                _sync_state["lastSuccessAt"] = now().isoformat()
                _sync_state["lastError"] = None
            else:
                _sync_state["lastError"] = "some items failed to sync"
            if to_put or to_del:
                log.info("B2 sync: %d uploaded/updated, %d deleted.", len(to_put), len(to_del))
            return not failed
        except Exception as exc:  # noqa: BLE001
            s.rollback()
            _sync_state["needsPush"] = True
            _sync_state["lastError"] = "sync failed"
            safe_log_error("B2 sync failed", exc)
            return False
        finally:
            _tls.quiet = False
            s.close()
            SessionLocal.remove()


def _timer_fired():
    global _sync_timer
    _sync_timer = None
    push_changes_to_b2()


def schedule_snapshot():
    """Throttled: at most one upload run per B2_SYNC_DEBOUNCE_SECONDS."""
    global _sync_timer
    if not B2_AUTOSYNC or getattr(_tls, "quiet", False) or not b2_configured() or _sync_state["blocked"]:
        return
    if _sync_timer is None:
        _sync_timer = threading.Timer(B2_SYNC_DEBOUNCE_SECONDS, _timer_fired)
        _sync_timer.daemon = True
        _sync_timer.start()


@event.listens_for(Session, "after_commit")
def _mark_dirty_after_commit(_session):
    schedule_snapshot()


def _flush_on_exit():
    # Upload anything still waiting when the server shuts down.
    if _sync_timer is not None:
        try:
            _sync_timer.cancel()
        except Exception:  # noqa: BLE001
            pass
        push_changes_to_b2()


atexit.register(_flush_on_exit)


# ---------------------------------------------------------------- B2 -> system

def _bump(row, by="b2-sync"):
    row.rev = (row.rev or 1) + 1
    row.updated_by = by


def _upsert_record(s, store, rid, data):
    data = dict(data or {})
    data["id"] = rid
    row = s.query(Record).filter_by(store=store, id=rid).first()
    if row is None:
        s.add(Record(id=rid, store=store, data=data, rev=1, updated_by="b2-sync",
                     **_extract_indexed_fields(data)))
        return "added"
    if row.deleted or (row.data or {}) != data:
        row.data = data
        row.deleted = False
        for k, v in _extract_indexed_fields(data).items():
            setattr(row, k, v)
        _bump(row)
        return "changed"
    return None


def _apply_user(s, u):
    row = s.query(User).filter_by(id=u["id"]).first()
    if row is None:
        if s.query(User).filter_by(username=u["username"]).first():
            return None
        s.add(User(id=u["id"], username=u["username"], password_hash=u["passwordHash"],
                   name=u["name"], role=u["role"], active=u.get("active", True),
                   photo=u.get("photo"), linked_teacher_id=u.get("linkedTeacherId"),
                   linked_student_ids=u.get("linkedStudentIds") or [],
                   must_change=u.get("mustChange", False)))
        return "added"
    row.password_hash = u["passwordHash"]; row.name = u["name"]; row.role = u["role"]
    row.active = u.get("active", True); row.photo = u.get("photo")
    row.linked_teacher_id = u.get("linkedTeacherId")
    row.linked_student_ids = u.get("linkedStudentIds") or []
    row.must_change = u.get("mustChange", False); row.deleted = False
    return "changed"


def _soft_delete_members(s, store, ids):
    n = 0
    for rid in ids or []:
        if store == "users":
            row = s.query(User).filter_by(id=rid, deleted=False).first()
            # Administrator accounts are never removed by a change made in B2.
            if row and row.role != "Administrator":
                row.deleted = True; row.active = False; _bump(row); n += 1
        else:
            row = s.query(Record).filter_by(store=store, id=rid, deleted=False).first()
            if row:
                row.deleted = True; _bump(row); n += 1
    return n


def _apply_payload(s, payload, prev_members):
    """Apply one downloaded B2 file. Returns (member ids, added, changed, removed)."""
    store = payload.get("store")
    added = changed = removed = 0
    if store == "users":
        u = payload["data"]
        res = _apply_user(s, u)
        return [u["id"]], int(res == "added"), int(res == "changed"), 0
    if store not in STORES:
        raise ValueError("unknown store in B2 file")
    if "records" in payload:
        members = []
        for d in payload["records"]:
            members.append(d["id"])
            res = _upsert_record(s, store, d["id"], d)
            added += res == "added"; changed += res == "changed"
        gone = [m for m in (prev_members or []) if m not in members]
        removed = _soft_delete_members(s, store, gone)
        return members, added, changed, removed
    res = _upsert_record(s, store, payload["id"], payload["data"])
    return [payload["id"]], int(res == "added"), int(res == "changed"), 0


def _list_b2_data(client):
    listing = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=B2_BUCKET_NAME, Prefix=DATA_PREFIX):
        for o in page.get("Contents", []):
            parts = o["Key"].split("/")
            if len(parts) == 3 and parts[1] in STORE_FOR_FOLDER and parts[2].endswith(".json"):
                listing[o["Key"]] = _norm_etag(o.get("ETag"))
    return listing


def pull_changes_from_b2(force=False):
    """Make the system match the bucket (files deleted/edited/added in B2)."""
    if not b2_configured() or _sync_state["blocked"]:
        return False
    if not force and _sync_timer is not None:
        return False  # local changes still waiting to upload — never overwrite them
    with _sync_lock:
        client = b2_client()
        listing = _list_b2_data(client)
        _tls.quiet = True
        s = SessionLocal()
        try:
            idx = {r.key: r for r in s.query(B2Index).all()}
            if not listing and idx:
                log.warning("No data files found in B2 but the system has data — not deleting anything; "
                            "they will be re-created.")
                _sync_state["needsPush"] = True
                return False
            deleted_keys = [k for k in idx if k not in listing]
            todo = [k for k, e in listing.items() if k not in idx or idx[k].etag != e]
            if not deleted_keys and not todo:
                _sync_state["lastPullAt"] = now().isoformat()
                return True

            def _get(k):
                obj = client.get_object(Bucket=B2_BUCKET_NAME, Key=k)
                return k, json.loads(obj["Body"].read().decode("utf-8"))

            fetched = {}
            with ThreadPoolExecutor(max_workers=8) as ex:
                for fut in [ex.submit(_get, k) for k in todo]:
                    try:
                        k, payload = fut.result()
                        fetched[k] = payload
                    except Exception as exc:  # noqa: BLE001
                        safe_log_error("Could not read a B2 item (skipped)", exc)

            removed = added = changed = 0
            for k in deleted_keys:
                row = idx[k]
                removed += _soft_delete_members(s, row.store, row.members)
                s.delete(row)
            for k, payload in fetched.items():
                row = idx.get(k)
                try:
                    members, a, c, r_ = _apply_payload(s, payload, row.members if row else [])
                except Exception as exc:  # noqa: BLE001
                    safe_log_error("Could not apply a B2 item (skipped)", exc)
                    continue
                added += a; changed += c; removed += r_
                if row is None:
                    row = B2Index(key=k)
                    s.add(row)
                row.store, row.members = payload["store"], members
                row.hash, row.etag = _payload_hash(payload), listing[k]
            s.commit()
            _sync_state["lastPullAt"] = now().isoformat()
            if added or changed or removed:
                log.info("Applied changes from Backblaze: %d added, %d updated, %d removed.",
                         added, changed, removed)
            return True
        except Exception:
            s.rollback()
            raise
        finally:
            _tls.quiet = False
            s.close()
            SessionLocal.remove()


def _restore_legacy_snapshot():
    """Older single-file backup (data/latest.json.gz) -> system. Used once."""
    try:
        obj = b2_client().get_object(Bucket=B2_BUCKET_NAME, Key=LEGACY_SNAPSHOT_KEY)
    except ClientError as exc:
        if str(exc.response.get("Error", {}).get("Code", "")) in ("NoSuchKey", "404", "NotFound"):
            return False
        raise
    snap = json.loads(gzip.decompress(obj["Body"].read()).decode("utf-8"))
    _tls.quiet = True
    s = SessionLocal()
    try:
        for u in snap.get("users", []):
            if not u.get("deleted"):
                _apply_user(s, u)
        for r in snap.get("records", []):
            if not r.get("deleted") and r.get("store") in STORES:
                _upsert_record(s, r["store"], r["id"], r.get("data") or {})
        s.commit()
        return True
    except Exception:
        s.rollback()
        raise
    finally:
        _tls.quiet = False
        s.close()
        SessionLocal.remove()


def restore_from_b2_if_empty():
    """Fresh/empty local database + data in B2  ->  rebuild from B2."""
    if not b2_configured():
        return
    s = SessionLocal()
    try:
        empty = not (s.query(Record).first() or s.query(User).first())
    finally:
        s.close()
        SessionLocal.remove()
    if not empty:
        return
    try:
        pull_changes_from_b2(force=True)
        s = SessionLocal()
        try:
            has_data = bool(s.query(Record).first() or s.query(User).first())
        finally:
            s.close()
            SessionLocal.remove()
        if not has_data:
            has_data = _restore_legacy_snapshot()
        if has_data:
            _sync_state["restoredFromB2"] = True
            log.info("Restored the system from Backblaze B2.")
        else:
            log.info("No data in B2 yet — starting with an empty database.")
    except Exception as exc:  # noqa: BLE001
        # Do NOT let a fresh empty database overwrite good data in B2.
        _sync_state["blocked"] = True
        _sync_state["lastError"] = "restore failed"
        safe_log_error("B2 restore failed — automatic sync disabled until restart", exc)


def reconcile_files_with_b2():
    """A document deleted directly in B2 disappears from the system too."""
    client = b2_client()
    keys = set()
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=B2_BUCKET_NAME):
        for o in page.get("Contents", []):
            keys.add(o["Key"])
    s = SessionLocal()
    try:
        gone = [r for r in s.query(Record).filter_by(store="files", deleted=False).all()
                if (r.data or {}).get("key") and r.data["key"] not in keys]
        if not gone:
            return
        gone_ids = {r.id for r in gone}
        for r in gone:
            r.deleted = True; _bump(r)
        for d in s.query(Record).filter_by(store="staffDocuments", deleted=False).all():
            if (d.data or {}).get("fileId") in gone_ids:
                d.deleted = True; _bump(d)
        s.commit()  # this commit also schedules a fresh sync
        log.info("Removed %d file record(s) whose file no longer exists in B2.", len(gone))
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("File reconcile failed", exc)
    finally:
        s.close()
        SessionLocal.remove()


def _poll_loop():
    n = 0
    time.sleep(5)
    while True:
        try:
            if _sync_timer is None and not _sync_state["blocked"]:
                if _sync_state["needsPush"]:
                    push_changes_to_b2()
                pull_changes_from_b2()
                n += 1
                if n % FILE_RECONCILE_EVERY_N_POLLS == 0:
                    reconcile_files_with_b2()
        except Exception as exc:  # noqa: BLE001
            safe_log_error("B2 poll failed", exc)
        time.sleep(B2_POLL_SECONDS)


def start_b2_poller():
    global _poll_started
    if _poll_started or not B2_AUTOSYNC or not b2_configured():
        return
    _poll_started = True
    threading.Thread(target=_poll_loop, name="b2-poller", daemon=True).start()


@app.route("/api/backups/sync-now", methods=["POST"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def sync_now():
    if not b2_configured():
        return json_error("File storage is not configured on this server.", 503)
    if _sync_state["blocked"]:
        return json_error("Automatic sync is paused because the last restore failed. Check the server log.", 503)
    ok = push_changes_to_b2()
    if not ok:
        return json_error("Upload to Backblaze failed.", 502)
    return jsonify({"ok": True, "at": _sync_state["lastSuccessAt"]})


# ==============================================================================
# 18. SYNC — server-authoritative delta sync for the offline IndexedDB layer
# ==============================================================================


# ==============================================================================
# 18a. FAST LOAD + BULK WRITE
#   /api/bootstrap  — every store the caller may read, in ONE round trip (the old login
#                     flow made ~28 separate requests).
#   /api/bulk/<path> — apply many upserts/deletes to one store in ONE request and ONE
#                     commit (timetable generation used to make hundreds of requests).
# ==============================================================================

ACTIVITY_BOOTSTRAP_LIMIT = 2000   # newest N log rows; older rows stay in the database
BULK_STORES = {"timetable", "timetableRules"}
BULK_MAX_ITEMS = 5000


def _role_allows(u, override, module):
    if override and u.role in override:
        return module in (override.get(u.role) or [])
    return module in ROLE_MODULES.get(u.role, [])


@app.route("/api/bootstrap", methods=["GET"])
@login_required
def bootstrap():
    u = current_user()
    s = db()
    override = get_grading_permissions_map()          # one query, not one per store
    stores, forbidden = {}, []
    for store in STORES:
        if store == "files":
            continue
        module = STORE_MODULE.get(store, store)
        if store not in PUBLIC_READ_STORES and not _role_allows(u, override, module) and not ref_read_ok(u, store):
            stores[store] = []
            forbidden.append(store)
            continue
        q = s.query(Record).filter_by(store=store, deleted=False)
        if store == "activity":
            q = q.order_by(Record.updated_at.desc()).limit(ACTIVITY_BOOTSTRAP_LIMIT)
        rows = q.all()
        scope = build_scope_filter(store, u)
        if scope:
            rows = [r for r in rows if scope(r)]
        stores[store] = [r.to_dict() for r in rows]
    # users are a separate table and only Administrators may list them
    if u.role == "Administrator":
        stores["users"] = [x.to_public_dict() for x in s.query(User).filter_by(deleted=False).all()]
    else:
        stores["users"] = []
        forbidden.append("users")
    return jsonify({"stores": stores, "forbidden": forbidden, "serverTime": now().isoformat()})


STORE_FROM_PATH = {}


def _store_for_path(path):
    if not STORE_FROM_PATH:
        for st in STORES:
            STORE_FROM_PATH[store_url_path(st)] = st
    return STORE_FROM_PATH.get(path)


@app.route("/api/bulk/<path>", methods=["POST"])
@login_required
@csrf_protect
def bulk_write(path):
    u = current_user()
    store = _store_for_path(path)
    if store not in BULK_STORES:
        return json_error("Bulk writes are not available for this resource.", 404)
    module = STORE_MODULE.get(store, store)
    if not role_has_module(u.role, module):
        return json_error("You do not have access to this module.", 403)
    if store in WRITE_RESTRICTED_STORES:
        return json_error("This resource is read-only via the API.", 403)
    if store in ADMIN_ONLY_WRITE_STORES and u.role not in PRIVILEGED_STAFF_ROLES:
        return json_error("Only administrators may modify this resource.", 403)

    payload = request.get_json(silent=True) or {}
    upserts = payload.get("upserts") or []
    delete_ids = payload.get("deleteIds") or []
    if not isinstance(upserts, list) or not isinstance(delete_ids, list):
        return json_error("upserts and deleteIds must be lists.", 400)
    if len(upserts) + len(delete_ids) > BULK_MAX_ITEMS:
        return json_error(f"Too many items in one request (max {BULK_MAX_ITEMS}).", 413)
    for item in upserts:
        if not isinstance(item, dict) or not item.get("id"):
            return json_error("Every upsert needs an id.", 400)

    # Timetable safety net (mirrors the client): one class + one day = at most ONE double lesson.
    # A double is stored as two consecutive period records; the first carries dbl == "start".
    if store == "timetable":
        double_starts = {}
        for item in upserts:
            if item.get("dbl") == "start":
                key = (item.get("classId"), item.get("day"), item.get("year"), item.get("term"))
                double_starts[key] = double_starts.get(key, 0) + 1
                if double_starts[key] > 1:
                    return json_error(
                        "Rejected: a class can have at most one double lesson per day "
                        f"({item.get('day')}). Nothing was saved.", 422)

    s = db()
    try:
        ids = [x["id"] for x in upserts] + [str(x) for x in delete_ids]
        existing = {}
        # chunked IN() so very large batches stay under SQLite's variable limit
        for i in range(0, len(ids), 500):
            chunk = ids[i:i + 500]
            for r in s.query(Record).filter(Record.store == store, Record.id.in_(chunk)).all():
                existing[r.id] = r

        removed = 0
        for rid in delete_ids:
            rec = existing.get(str(rid))
            if rec and not rec.deleted:
                rec.deleted = True
                rec.rev = (rec.rev or 1) + 1
                rec.updated_by = u.username
                removed += 1

        saved = []
        for item in upserts:
            rid = item["id"]
            data = dict(item)
            for k in ("_rev", "_updatedAt", "_deleted"):
                data.pop(k, None)
            fields = _extract_indexed_fields(data)
            rec = existing.get(rid)
            if rec:
                rec.data = data
                rec.deleted = False
                rec.rev = (rec.rev or 1) + 1
                rec.updated_by = u.username
                for k, v in fields.items():
                    setattr(rec, k, v)
            else:
                rec = Record(id=rid, store=store, data=data, rev=1,
                             updated_by=u.username, **fields)
                s.add(rec)
                existing[rid] = rec
            saved.append(rec)
        s.commit()
        out = [r.to_dict() for r in saved]
    except Exception as exc:  # noqa: BLE001
        s.rollback()
        safe_log_error("bulk_write failed", exc)
        return json_error("Bulk save failed; no changes were applied.", 500)

    log_activity(f"{u.name} bulk-saved {len(out)} and removed {removed} {store} record(s)", u)
    return jsonify({"saved": out, "removed": removed, "serverTime": now().isoformat()})


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
        db().execute(text("SELECT 1"))
        status["database"] = "ok"
    except Exception as exc:  # noqa: BLE001
        safe_log_error("health check DB failure", exc)
        status["database"] = "error"
        status["status"] = "degraded"
    status["fileStorage"] = "configured" if b2_configured() else "not_configured"
    status["b2Autosync"] = {"enabled": B2_AUTOSYNC and b2_configured(), "lastSuccessAt": _sync_state["lastSuccessAt"],
                            "lastPullAt": _sync_state["lastPullAt"], "paused": _sync_state["blocked"], "restoredFromB2": _sync_state["restoredFromB2"]}
    code = 200 if status["status"] == "ok" else 503
    return jsonify(status), code


# ==============================================================================
# 21. SERVE THE EXISTING FRONTEND (index.html + any sibling static assets)
# ==============================================================================

_index_cache = {"mtime": None, "raw": b"", "gz": b"", "etag": ""}


def _load_index_cached():
    path = os.path.join(FRONTEND_DIR, FRONTEND_INDEX)
    st = os.stat(path)
    if _index_cache["mtime"] != st.st_mtime_ns:
        with open(path, "rb") as fh:
            raw = fh.read()
        _index_cache.update(mtime=st.st_mtime_ns, raw=raw,
                            gz=gzip.compress(raw, compresslevel=6),
                            etag='"%s"' % hashlib.md5(raw).hexdigest())
    return _index_cache


@app.route("/", methods=["GET"])
def serve_index():
    # The app is one ~0.5 MB HTML file. Send it gzipped (~5x smaller) and let the browser
    # revalidate with an ETag, so repeat visits cost a tiny 304 instead of a full download.
    try:
        c = _load_index_cached()
    except OSError:
        return send_from_directory(FRONTEND_DIR, FRONTEND_INDEX)
    if request.headers.get("If-None-Match") == c["etag"]:
        resp = Response(status=304)
        resp.headers["ETag"] = c["etag"]
        resp.headers["Cache-Control"] = "no-cache"
        return resp
    use_gz = "gzip" in (request.headers.get("Accept-Encoding") or "")
    resp = Response(c["gz"] if use_gz else c["raw"], mimetype="text/html")
    if use_gz:
        resp.headers["Content-Encoding"] = "gzip"
    resp.headers["Vary"] = "Accept-Encoding"
    resp.headers["ETag"] = c["etag"]
    resp.headers["Cache-Control"] = "no-cache"
    return resp


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
def compress_json_responses(resp):
    """gzip JSON API responses over 1 KB (a full data load is ~5-10x smaller on the wire)."""
    try:
        if (resp.status_code == 200 and not resp.direct_passthrough
                and (resp.mimetype or "") == "application/json"
                and "gzip" in (request.headers.get("Accept-Encoding") or "")
                and "Content-Encoding" not in resp.headers):
            data = resp.get_data()
            if len(data) > 1024:
                resp.set_data(gzip.compress(data, compresslevel=5))
                resp.headers["Content-Encoding"] = "gzip"
                resp.headers["Vary"] = "Accept-Encoding"
    except Exception:  # noqa: BLE001 - never let compression break a response
        pass
    return resp


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
    restore_from_b2_if_empty()   # empty DB + snapshot in B2 -> restore it
    seed_if_empty()
    start_b2_poller()            # watch B2 for changes made directly there
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
