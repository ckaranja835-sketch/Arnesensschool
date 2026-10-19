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
    "formative", "examResources", "smsLog", "reportRemarks",
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
    "Deputy Head Teacher": ["dashboard", "attendance", "exams", "marks", "analysis", "discipline",
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
    "reportRemarks": "reportcards",   # term-specific class/head teacher remarks live under Report Cards
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
                          "feePayments", "discipline", "borrows", "leaveOuts", "reportRemarks"}
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
                 "combined", "reportcards", "marks"},
    "exams": {"marks"},
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
        if store in ("marks", "attendance", "feePayments", "discipline", "borrows", "leaveOuts", "reportRemarks"):
            return lambda r: (r.data or {}).get("studentId") in own_ids
        if store == "feeStructures":
            own_sections = {_class_section(c) for c in own_classes if c}
            return lambda r: ((r.data or {}).get("classId") in own_classes
                              or ((r.data or {}).get("section") and not (r.data or {}).get("classId")
                                  and (r.data or {}).get("section") in own_sections))

    if user.role in TEACHER_ROLES and store in TEACHER_SCOPED_STORES and user.role not in PRIVILEGED_STAFF_ROLES:
        # Marks Entry is open to every class/subject for teachers, so the pupil roll and the marks
        # records are readable school-wide (pupil rows outside the teacher's own classes are reduced
        # to a minimal view by serialize_for_user). Attendance, discipline, notes and formative stay scoped.
        if store in ("students", "marks"):
            return None
        visible_classes = teacher_visible_class_ids(user)
        if store in ("attendance", "formative"):
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


_SLIM_STUDENT_KEYS = ("id", "admissionNo", "firstName", "middleName", "lastName", "classId",
                      "status", "gender", "_rev", "_updatedAt", "_deleted")


def serialize_for_user(user, store, rec, visible_classes=None):
    """rec.to_dict(), except that for Class/Subject Teachers a pupil outside their own classes is
    reduced to the fields Marks Entry needs (no contacts, medical, fee or guardian data)."""
    d = rec.to_dict()
    if store == "students" and user.role in TEACHER_ROLES and user.role not in PRIVILEGED_STAFF_ROLES:
        if visible_classes is None:
            visible_classes = teacher_visible_class_ids(user)
        if d.get("classId") not in visible_classes:
            return {k: d[k] for k in _SLIM_STUDENT_KEYS if k in d}
    return d


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
    "reportRemarks": "report-remarks",
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
        vis = teacher_visible_class_ids(u) if (store == "students" and u.role in TEACHER_ROLES) else None
        items, total = query_store(store, request.args, scope_filter=scope)
        return jsonify({
            "items": [serialize_for_user(u, store, r, vis) for r in items],
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
        if not err and store == "reportRemarks":
            err = check_report_remark_write(u, payload, payload.get("id"))
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
        return jsonify(serialize_for_user(u, store, rec))

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
        if not err and store == "reportRemarks":
            err = check_report_remark_write(u, payload, record_id, existing)
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


REPORT_REMARK_TERMS = ("Term 1", "Term 2", "Term 3")
REPORT_REMARK_MAX_LEN = 1500


def report_remark_id(student_id, year, term):
    """Deterministic id: exactly one record per pupil + academic year + term, so saving
    Term 1 can never overwrite Term 2 or Term 3. Must mirror rcRemarkId() in the frontend."""
    return "rr_%s_%s_%s" % (student_id, year, re.sub(r"\s+", "", str(term or "")))


def check_report_remark_write(user, payload, record_id, existing=None):
    """Validate + sanitise a term remark write. Returns an error string or None.
    Mutates payload so a non-head-teacher can never change the Head Teacher's remark."""
    if user.role in STUDENT_PORTAL_ROLES:
        return "You cannot edit report card remarks."
    sid = str(payload.get("studentId") or "").strip()
    year = str(payload.get("year") or "").strip()
    term = str(payload.get("term") or "").strip()
    if not sid or not year:
        return "Pupil and academic year are required."
    if term not in REPORT_REMARK_TERMS:
        return "Term must be Term 1, Term 2 or Term 3."
    if record_id != report_remark_id(sid, year, term):
        return "Remark record does not match its pupil, year and term."
    for k in ("classTeacherRemark", "headTeacherRemark"):
        v = payload.get(k, "")
        if v is None:
            v = ""
        if not isinstance(v, str):
            return "Remarks must be text."
        if len(v) > REPORT_REMARK_MAX_LEN:
            return "Remarks must be %d characters or fewer." % REPORT_REMARK_MAX_LEN
        payload[k] = v.strip()
    if user.role not in PRIVILEGED_STAFF_ROLES:
        # Class teachers: only their own class's pupils, and only the class-teacher remark.
        if user.role in TEACHER_ROLES and student_class_id(sid) not in teacher_visible_class_ids(user):
            return "You can only enter remarks for pupils in your own class."
        payload["headTeacherRemark"] = ((existing.data or {}).get("headTeacherRemark", "") if existing else "")
    return None


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

    # Universal marks entry: Administrators, Head/Deputy Head Teachers and Class/Subject Teachers may
    # enter marks for ANY class and subject. Access is still gated by the "marks" module above, plus
    # authentication and CSRF. Only the referenced records must be real.
    s = db()
    if not _record_data("exams", exam_id):
        return json_error("Unknown examination.", 400)
    if not _record_data("subjects", subject_id):
        return json_error("Unknown subject.", 400)
    saved, errors = [], []
    try:
        for entry in entries:
            student_id = entry.get("studentId")
            if not student_id:
                errors.append({"entry": entry, "error": "Missing studentId"})
                continue
            if not _record_data("students", student_id):
                errors.append({"entry": entry, "error": "Unknown pupil"})
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
# 12b. AI MARKSHEET SCANNING  (Marks Entry → "Scan Paper Marksheet")
# ------------------------------------------------------------------------------
# Privacy model (CRITICAL):
#   * Default (browser OCR): the photo never leaves the teacher's device. Only OCR word boxes (text +
#     positions) are POSTed to /api/marksheet-scan/structure.
#   * Server OCR (optional): a marksheet photo arrives as a raw request body (no multipart spooling),
#     is validated, normalised in memory, written ONCE to a private 0700 temp
#     directory outside the web root under a random name, handed to the OCR
#     provider, and deleted in a `finally:` block whether OCR succeeds, fails,
#     times out or the client disconnects. The response states whether deletion
#     was verified. Images are never put in the database, B2, logs or backups.
#   * A janitor thread (and a purge at boot / exit) removes anything abandoned
#     by a crash or kill -9.
#   * Only extracted text/numbers are returned. The server keeps NO scan session:
#     matching/validation endpoints are stateless and receive the (teacher-
#     corrected) extracted rows, never images.
#   * Marks reach the official `marks` store ONLY through /api/marksheet-scan/
#     import, which re-validates everything server-side.
#
# Environment variables (all optional unless stated):
#   SCAN_OCR_PROVIDER      (optional) unset / "browser" = FREE OCR inside the teacher's browser (Tesseract.js,
#                          open source, no API key, no server install; the default). "tesseract" = free OCR on
#                          this server instead (needs the tesseract-ocr package, i.e. a Docker deploy on Render).
#                          Old values such as "anthropic" are ignored; no paid service is ever contacted.
#   SCAN_TMP_DIR           private temp dir (default: <system tmp>/arnesens_scan_tmp)
#   SCAN_MAX_IMAGE_MB      per-image limit after browser compression (default 8)
#   SCAN_MAX_IMAGES        max photos per session, enforced client-side too (default 20)
#   SCAN_OCR_TIMEOUT       seconds before a provider call is abandoned (default 90)
#   SCAN_TMP_MAX_AGE       seconds before the janitor deletes a stray temp file (default 300)
#   SCAN_RESTRICT_TEACHERS "1" (default): Class/Subject Teachers may only import for
#                          classes/subjects they are assigned to. "0" = same universal
#                          access as manual Marks Entry.
# ==============================================================================

import base64
import urllib.error
import shutil
import subprocess
import tempfile
import uuid
import difflib

SCAN_OCR_PROVIDER = (env("SCAN_OCR_PROVIDER", "") or "").lower()   # "", "browser" or "tesseract"; anything else = browser
SCAN_OCR_VENDOR_DIR = os.path.join(FRONTEND_DIR, "vendor", "tesseract")   # optional self-hosted Tesseract.js assets
SCAN_OCR_VENDOR_FILES = ("tesseract.min.js", "worker.min.js", "eng.traineddata.gz", "tesseract-core.wasm.js")
SCAN_MAX_IMAGE_BYTES = int(float(env("SCAN_MAX_IMAGE_MB", "8")) * 1024 * 1024)
SCAN_MAX_IMAGES = int(env("SCAN_MAX_IMAGES", "20"))
SCAN_OCR_TIMEOUT = int(env("SCAN_OCR_TIMEOUT", "90"))
SCAN_TMP_MAX_AGE = max(int(env("SCAN_TMP_MAX_AGE", "300")), SCAN_OCR_TIMEOUT + 60)
SCAN_RESTRICT_TEACHERS = env("SCAN_RESTRICT_TEACHERS", "1") != "0"
SCAN_MAX_DIM = 2200            # longest edge after server-side normalisation
SCAN_SETTINGS_ID = "scanSettings"
SCAN_DEFAULT_SETTINGS = {"admissionWidth": 4, "confidenceThreshold": 0.8, "abbreviations": []}

try:
    from PIL import Image, ImageOps
    PIL_AVAILABLE = True
except Exception:  # pragma: no cover
    PIL_AVAILABLE = False


def _scan_resolve_tmp_dir():
    wanted = env("SCAN_TMP_DIR") or os.path.join(tempfile.gettempdir(), "arnesens_scan_tmp")
    wanted = os.path.abspath(wanted)
    web_root = os.path.abspath(FRONTEND_DIR)
    # Never allow the temp dir to live inside anything the static route could serve.
    if wanted == web_root or wanted.startswith(web_root + os.sep):
        wanted = os.path.join(tempfile.gettempdir(), "arnesens_scan_tmp")
    return wanted


SCAN_TMP_DIR = _scan_resolve_tmp_dir()
_scan_lock = threading.Lock()
_scan_live_files = set()       # paths currently being processed (for janitor / tests)
_scan_import_cache = {}        # importToken -> (timestamp, result)  (double-click guard)


def _scan_ensure_tmp_dir():
    os.makedirs(SCAN_TMP_DIR, mode=0o700, exist_ok=True)
    try:
        os.chmod(SCAN_TMP_DIR, 0o700)
    except OSError:
        pass


def scan_secure_delete(path):
    """Best-effort overwrite then unlink. Returns True only if the file is verifiably gone."""
    try:
        if os.path.isfile(path):
            try:
                size = os.path.getsize(path)
                with open(path, "r+b", buffering=0) as fh:
                    fh.write(b"\x00" * min(size, 64 * 1024 * 1024))
                    fh.flush()
                    os.fsync(fh.fileno())
            except OSError:
                pass  # overwrite is best-effort; the unlink below is what matters
            os.remove(path)
    except FileNotFoundError:
        pass
    except OSError as exc:
        safe_log_error("scan temp delete failed", exc)
    with _scan_lock:
        _scan_live_files.discard(path)
    return not os.path.exists(path)


def scan_purge_tmp(max_age=None):
    """Delete stray temp files. max_age=None deletes everything (boot / exit)."""
    removed = 0
    try:
        if not os.path.isdir(SCAN_TMP_DIR):
            return 0
        cutoff = None if max_age is None else time.time() - max_age
        for name in os.listdir(SCAN_TMP_DIR):
            p = os.path.join(SCAN_TMP_DIR, name)
            try:
                if os.path.isfile(p) and (cutoff is None or os.path.getmtime(p) < cutoff):
                    if scan_secure_delete(p):
                        removed += 1
            except OSError:
                continue
    except OSError as exc:
        safe_log_error("scan purge failed", exc)
    return removed


def _scan_janitor_loop():
    while True:
        time.sleep(120)
        try:
            n = scan_purge_tmp(SCAN_TMP_MAX_AGE)
            if n:
                log.info("Marksheet scan janitor removed %d abandoned temp file(s).", n)
        except Exception as exc:  # noqa: BLE001
            safe_log_error("scan janitor", exc)


def scan_startup():
    _scan_ensure_tmp_dir()
    n = scan_purge_tmp(None)          # nothing may survive a restart
    if n:
        log.info("Marksheet scan: purged %d temp file(s) left by a previous run.", n)
    threading.Thread(target=_scan_janitor_loop, name="scan-janitor", daemon=True).start()
    atexit.register(lambda: scan_purge_tmp(None))


# ---------------------------------------------------------------- settings ----

def scan_get_settings():
    rec = db().query(Record).filter_by(store="settings", id=SCAN_SETTINGS_ID, deleted=False).first()
    d = dict(SCAN_DEFAULT_SETTINGS)
    if rec and isinstance(rec.data, dict):
        d.update({k: v for k, v in rec.data.items() if k in SCAN_DEFAULT_SETTINGS})
    d["abbreviations"] = [a for a in (d.get("abbreviations") or []) if isinstance(a, dict)]
    return d


def scan_norm_header(text):
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def scan_validate_settings(payload, subject_ids):
    """Returns (clean_settings, error_message)."""
    try:
        width = int(payload.get("admissionWidth", 4))
    except (TypeError, ValueError):
        return None, "Admission-number width must be a whole number."
    if not 2 <= width <= 8:
        return None, "Admission-number width must be between 2 and 8 digits."
    thr = to_float(payload.get("confidenceThreshold"), 0.8)
    if not 0.5 <= thr <= 0.99:
        return None, "Confidence threshold must be between 0.50 and 0.99."
    seen, clean = {}, []
    for a in payload.get("abbreviations") or []:
        abbr = str(a.get("abbr", "")).strip()[:16]
        key = scan_norm_header(abbr)
        if not key:
            return None, "Every abbreviation needs letters or digits."
        sid = a.get("subjectId")
        if sid not in subject_ids:
            return None, f"Abbreviation “{abbr}” points to a subject that does not exist."
        if key in seen and seen[key] != sid:
            return None, f"“{abbr}” is mapped to two different subjects. Each abbreviation may map to only one."
        mx = a.get("maxMarks")
        if mx in (None, ""):
            mx = None
        else:
            mx = to_float(mx, -1)
            if not 0 < mx <= 1000:
                return None, f"Mark limit for “{abbr}” must be between 1 and 1000."
        if key in seen:
            continue                      # exact duplicate row — drop silently
        seen[key] = sid
        clean.append({"id": a.get("id") or new_id("abbr"), "abbr": abbr, "subjectId": sid, "maxMarks": mx})
    return {"admissionWidth": width, "confidenceThreshold": thr, "abbreviations": clean}, None


# ------------------------------------------------------- pure matching logic ----
# These functions take plain data and touch no database, so they are unit-testable.

def scan_normalize_admission(raw, width):
    """Paper number -> registered format. Returns (normalized | None, status).
    Always handled as a STRING. '1'->'0001', '158'->'0158', '951'->'0951', '0001'->'0001', '1234'->'1234'.
    More digits than `width` (e.g. '12345', '00158') is never truncated or trimmed: it is 'invalid' and the
    teacher must correct it. '0'/'0000' is never a real admission number."""
    s = re.sub(r"[\s.\-_/]", "", str(raw if raw is not None else ""))
    if not s:
        return None, "missing"
    if not re.fullmatch(r"[0-9]+", s):
        return None, "invalid"
    if len(s) > width:
        return None, "invalid"           # longer than the school format: do not truncate
    if not s.strip("0"):
        return None, "invalid"           # "0" / "0000" is never a real admission number
    return s.zfill(width), "ok"


def _scan_tokens(name):
    return [t for t in re.split(r"[^a-z0-9]+", str(name or "").lower()) if t]


def scan_name_score(a, b):
    ta, tb = _scan_tokens(a), _scan_tokens(b)
    if not ta or not tb:
        return 0.0
    hits = 0
    for t in ta:
        if max(difflib.SequenceMatcher(None, t, u).ratio() for u in tb) >= 0.8:
            hits += 1
    token_score = hits / max(len(ta), 1)
    whole = difflib.SequenceMatcher(None, " ".join(sorted(ta)), " ".join(sorted(tb))).ratio()
    return max(token_score, whole)


# Common heading abbreviations on Kenyan merit lists -> words that must ALL appear in the subject's name.
# Used only after saved abbreviations, subject codes and exact subject names, and only when exactly ONE
# existing subject fits. Zero or several fits stay unmapped for the teacher to choose (never guessed).
SCAN_STD_ABBR = {
    "MATH": ["MATH"], "MATHS": ["MATH"], "MAT": ["MATH"],
    "ENGL": ["ENGLISH"], "ENG": ["ENGLISH"], "ENGLISH": ["ENGLISH"],
    "KISW": ["KISWAHILI"], "KIS": ["KISWAHILI"], "KISWA": ["KISWAHILI"], "KSW": ["KISWAHILI"],
    "ISCI": ["INTEGRATED", "SCIENCE"], "INTSCI": ["INTEGRATED", "SCIENCE"],
    "SCI": ["SCIENCE"], "SCIE": ["SCIENCE"],
    "SST": ["SOCIAL", "STUDIES"], "SOCST": ["SOCIAL", "STUDIES"], "SOCIAL": ["SOCIAL", "STUDIES"],
    "CAS": ["CREATIVE", "ARTS"], "CA": ["CREATIVE", "ARTS"],
    "AGN": ["AGRICULTURE"], "AGR": ["AGRICULTURE"], "AGRI": ["AGRICULTURE"], "AGRIC": ["AGRICULTURE"],
    "REL": ["RELIGIOUS"], "RE": ["RELIGIOUS"], "CRE": ["CHRISTIAN"], "IRE": ["ISLAMIC"], "HRE": ["HINDU"],
    "PTC": ["PRE", "TECHNICAL"], "PRETECH": ["PRE", "TECHNICAL"],
}


def scan_resolve_columns(columns, subjects, abbreviations):
    """columns: [{key, header, subjectId?}] -> [{key, header, subjectId, status, subjectName, limit}]
    Order: user-chosen mapping > saved abbreviation > exact subject code > exact subject name.
    Unknown or ambiguous headers are NEVER guessed."""
    by_id = {s["id"]: s for s in subjects}
    abbr_map, limit_map = {}, {}
    for a in abbreviations:
        abbr_map.setdefault(scan_norm_header(a["abbr"]), set()).add(a["subjectId"])
        if a.get("maxMarks"):
            limit_map[a["subjectId"]] = a["maxMarks"]
    out = []
    for c in columns:
        sid, status, src = None, "unknown", ""
        chosen = c.get("subjectId")
        if chosen and chosen in by_id:
            sid, status = chosen, "mapped"
        else:
            h = scan_norm_header(c.get("header"))
            cands = set(abbr_map.get(h, set()))
            if not cands and h:
                cands = {s["id"] for s in subjects if scan_norm_header(s.get("code")) == h}
                if not cands:
                    cands = {s["id"] for s in subjects if scan_norm_header(s.get("name")) == h}
            source = "saved" if cands and h in abbr_map else "code/name"
            if not cands and h in SCAN_STD_ABBR:
                words = SCAN_STD_ABBR[h]
                cands = {s["id"] for s in subjects if all(w in scan_norm_header(s.get("name")) for w in words)}
                source = "standard"
            if len(cands) == 1:
                sid, status = next(iter(cands)), "mapped"
                src = source
            elif len(cands) > 1:
                status = "ambiguous"
        out.append({"key": c["key"], "header": c.get("header", ""), "subjectId": sid, "status": status,
                    "source": (("chosen" if chosen and chosen in by_id else src) if sid else ""),
                    "subjectName": by_id[sid]["name"] if sid else "", "limit": limit_map.get(sid) if sid else None})
    return out


_ABSENT_WORDS = {"AB", "ABS", "ABSENT"}
_DASHES = {"-", "_", "\u2013", "\u2014", "--"}
# A performance-level code written after the mark: EE1 EE2 ME1 ME2 AE1 AE2 BE1 BE2 (OCR often turns the digit into I/l/|).
_SCAN_PERF_TAIL = re.compile(r"(?i)\s*(?:EE|ME|AE|BE)\s*[12IlL|]?\s*$")
# A token that is a number once look-alike letters are fixed (O->0, l->1 ...). Must still contain a real digit.
_SCAN_NUMTOK = re.compile(r"^[0-9OoDlI|SsBZzG]+(?:[.,][0-9OoDlI|SsBZzG]{1,2})?$")
_SCAN_GLUED = re.compile(r"^(\d{1,3}(?:[.,]\d{1,2})?)([A-Za-z].*)$")


def scan_split_mark(text):
    """Split what a subject cell says into the numeric mark and the rest.
    '76 EE2'->76, '100 EE1'->100, '18 BE2'->18, '97EE1'->97, '-'->dash, ''->empty.
    Returns {raw, kind, num, code, substituted, multi}; kind in empty|absent|dash|number|nonnumeric.
    The performance code is NEVER a mark. A letter read in place of a digit (O for 0, l for 1 ...) is
    corrected only inside the mark itself and reported through `substituted` so a human checks it."""
    raw = str(text if text is not None else "")
    t = " ".join(raw.split())
    out = {"raw": raw.strip(), "kind": "empty", "num": None, "code": "", "substituted": False, "multi": False}
    if not t:
        return out
    if t.upper() in _ABSENT_WORDS:
        out["kind"] = "absent"; return out
    if re.fullmatch(r"[-_\u2013\u2014.\s]+", t):
        out["kind"] = "dash"; return out
    body, code = t, ""
    m = _SCAN_PERF_TAIL.search(body)
    if m and m.start() > 0:
        code, body = m.group().strip(), body[:m.start()].strip()
    toks = body.split(" ")
    first, rest = toks[0], toks[1:]
    num = None
    if _SCAN_NUMTOK.match(first) and re.search(r"[0-9]", first):
        num = first
    else:
        g = _SCAN_GLUED.match(first)
        if g:
            num, code = g.group(1), code or g.group(2)
    if num is None:
        out["kind"], out["code"] = "nonnumeric", code
        return out
    num = num.replace(",", ".")
    fixed = "".join(_SCAN_NUM_FIX.get(ch, ch) for ch in num)
    out["substituted"] = fixed != num
    num = fixed
    # any further token that is itself a number means two cells were merged ("76 97")
    for tok in rest:
        if _SCAN_NUMTOK.match(tok) and re.search(r"[0-9]", tok):
            out["multi"] = True
        elif not code:
            code = tok
    out.update(kind="number", num=num, code=code)
    return out


def scan_parse_mark(text, max_marks):
    """-> (kind, value, message) with kind in empty|score|absent|dash|invalid.
    A dash / blank is *missing* (never 0). A trailing performance code is ignored."""
    p = scan_split_mark(text)
    k = p["kind"]
    if k == "empty":
        return "empty", None, ""
    if k == "absent":
        return "absent", None, ""
    if k == "dash":
        return "dash", None, "A dash was written: the mark is missing, not zero. Nothing will be imported."
    if k == "nonnumeric":
        return "invalid", None, "No mark number could be read here."
    if p["multi"]:
        return "invalid", None, "Two numbers were read in one cell (neighbouring columns may have merged). Type the correct mark."
    if not re.fullmatch(r"\d+(\.\d{1,2})?", p["num"] or ""):
        return "invalid", None, "Not a valid number."
    v = float(p["num"])
    if v > max_marks:
        return "invalid", v, f"Above the maximum of {max_marks:g}."
    return "score", v, ""


def scan_match_rows(payload, ctx):
    """Core validation. ctx supplies: width, threshold, exam, class_id, students (all, by id),
    subjects, abbreviations, existing (dict (sid,subj)->mark data), can_subject(subj)->bool,
    privileged(bool). Pure — no DB access."""
    width, thr = ctx["width"], ctx["threshold"]
    exam_max = to_float(ctx["exam"].get("maxMarks"), 100) or 100
    students = ctx["students"]
    cols = scan_resolve_columns(payload.get("columns") or [], ctx["subjects"], ctx["abbreviations"])
    col_by_key = {c["key"]: c for c in cols}

    # Index students by their stored admission number AND by its four-digit form, so a school whose older
    # records were saved without leading zeros ("158") is still matched by the paper's "158" -> "0158".
    # Nothing is changed in the database: this is a lookup only. Two students that collapse to the same
    # four-digit key are reported as ambiguous, never guessed.
    by_adm = {}
    for s in students.values():
        a = str(s.get("admissionNo") or "").strip()
        if not a:
            continue
        keys = {a}
        nk, nst = scan_normalize_admission(a, width)
        if nst == "ok":
            keys.add(nk)
        for k in keys:
            lst = by_adm.setdefault(k, [])
            if s not in lst:
                lst.append(s)
    class_students = [s for s in students.values() if s.get("classId") == ctx["class_id"]
                      and s.get("status") not in ("Archived", "Transferred")]

    out_rows, claims = [], {}
    for row in payload.get("rows") or []:
        rid = row.get("rid")
        raw = str(row.get("admRaw") or "")
        r = {"rid": rid, "studentId": None, "studentName": "", "studentAdm": "", "normalizedAdm": None,
             "idStatus": "missing", "idMsg": "", "nameMismatch": False, "suggestions": [], "cells": {}}
        picked = row.get("studentId")
        cand, status, msg = None, None, ""
        if picked:
            cand = students.get(picked)
            status = "picked" if cand else "not_found"
            msg = "" if cand else "Chosen student no longer exists."
        else:
            norm, st = scan_normalize_admission(raw, width)
            r["normalizedAdm"] = norm
            if st == "missing":
                status, msg = "missing", "No admission number was read."
            elif st == "invalid":
                status, msg = "invalid", f"“{raw}” is not a valid admission number (expected up to {width} digits)."
            else:
                found = by_adm.get(norm, [])
                if len(found) == 1:
                    cand, status = found[0], "matched"
                elif len(found) > 1:
                    status, msg = "ambiguous", f"{norm} belongs to more than one student — choose the right one."
                else:
                    status, msg = "not_found", f"Student not found (looked for {norm})."
        if cand is not None:
            if cand.get("classId") != ctx["class_id"]:
                status = "not_in_class"
                msg = "This student is registered in a different class." if not ctx["privileged"] else \
                      f"{cand.get('firstName','')} {cand.get('lastName','')} is registered in a different class."
                cand = None
            elif cand.get("status") in ("Archived", "Transferred"):
                status, msg, cand = "not_in_class", "This student is archived or transferred.", None
        if cand is not None:
            r["studentId"] = cand["id"]
            r["studentName"] = f"{cand.get('firstName','')} {cand.get('lastName','')}".strip()
            r["studentAdm"] = cand.get("admissionNo", "")
            paper_name = row.get("name") or ""
            if status == "matched" and paper_name and scan_name_score(paper_name, r["studentName"]) < 0.5:
                r["nameMismatch"] = True
                r["idMsg"] = f"Paper says “{paper_name}” but admission {r['studentAdm']} is {r['studentName']}. Check it is the right pupil."
        else:
            # Suggestions only — never auto-selected.
            name = row.get("name") or ""
            if name:
                scored = sorted(((scan_name_score(name, f"{s.get('firstName','')} {s.get('lastName','')}"), s)
                                 for s in class_students), key=lambda x: -x[0])
                r["suggestions"] = [{"studentId": s["id"], "name": f"{s.get('firstName','')} {s.get('lastName','')}".strip(),
                                     "admissionNo": s.get("admissionNo", "")} for sc, s in scored[:4] if sc >= 0.55]
        r["idStatus"], r["idMsg"] = status, (msg or r["idMsg"])

        for key, cell in (row.get("cells") or {}).items():
            col = col_by_key.get(key)
            c = {"status": "ready", "msg": "", "score": None, "absent": False, "max": exam_max,
                 "existing": None, "existingAbsent": False}
            if cell.get("ignored"):
                c["status"] = "ignored"
            elif not col or col["status"] != "mapped":
                c["status"] = "unknown_subject"
                c["msg"] = "Map this column to a subject first."
            else:
                limit = min(exam_max, col["limit"]) if col.get("limit") else exam_max
                c["max"] = limit
                kind, val, pmsg = scan_parse_mark(cell.get("text"), limit)
                conf = to_float(cell.get("conf"), 0.0)
                confirmed = bool(cell.get("confirmed") or cell.get("edited"))
                parts = scan_split_mark(cell.get("text"))
                c["raw"] = str(cell.get("raw") if cell.get("raw") is not None else (cell.get("text") or ""))[:60]
                c["code"] = parts["code"]
                c["conf"] = conf
                if kind == "dash" and cell.get("asAbsent"):
                    kind, val, pmsg = "absent", None, ""      # the teacher explicitly chose "Record as absent"
                    confirmed = True
                if kind == "empty":
                    c["status"], c["msg"] = ("illegible", "Marked unreadable — type the mark.") if cell.get("illegible") \
                        else ("empty", "No mark written.")
                elif kind == "dash":
                    c["status"], c["msg"] = "missing", pmsg    # never zero, never imported unless the teacher opts in
                elif kind == "invalid":
                    c["status"], c["msg"] = "invalid", pmsg
                elif cell.get("illegible") and not confirmed:
                    c["status"], c["msg"] = "illegible", "Handwriting unclear — check and correct."
                elif kind in ("score", "absent") and not confirmed and (cell.get("flag") or parts["substituted"]):
                    c["status"], c["msg"] = "uncertain", str(cell.get("flag") or "A letter was read as a digit — check the mark against the paper.")[:140]
                elif kind in ("score", "absent") and conf < thr and not confirmed:
                    c["status"], c["msg"] = "uncertain", f"Low confidence ({int(conf*100)}%) — check against the paper."
                if kind in ("score", "absent") and c["status"] in ("ready", "uncertain"):
                    c["score"] = val
                    c["absent"] = kind == "absent"
                if not ctx["can_subject"](col["subjectId"]):
                    c["status"], c["msg"] = "forbidden", "You are not assigned to this subject for this class."
            if r["studentId"] is None and c["status"] in ("ready", "uncertain"):
                c["blockedByStudent"] = True
            if c["status"] in ("ready", "uncertain") and r["studentId"] and col and col.get("subjectId"):
                claims.setdefault((r["studentId"], col["subjectId"]), []).append((rid, key, c))
            r["cells"][key] = c
        out_rows.append(r)

    # duplicates / conflicts across rows, pages and repeated subject columns
    for (sid, subj), lst in claims.items():
        if len(lst) < 2:
            continue
        vals = {(c["absent"], c["score"]) for _, _, c in lst}
        if len(vals) == 1:
            for _, _, c in lst[1:]:
                c["status"], c["msg"] = "duplicate", "Same student and subject already read from another row/photo (same value)."
        else:
            for _, _, c in lst:
                c["status"], c["msg"] = "conflict", "This student and subject appear more than once with different marks — choose which is correct."
                c["conflictWith"] = [{"rid": r2, "key": k2, "text": ("AB" if c2["absent"] else c2["score"])} for r2, k2, c2 in lst]

    # compare with marks already saved in Marks Entry
    for r in out_rows:
        if not r["studentId"]:
            continue
        for key, c in r["cells"].items():
            col = col_by_key.get(key)
            if c["status"] not in ("ready", "uncertain") or not col or not col.get("subjectId"):
                continue
            ex = ctx["existing"].get((r["studentId"], col["subjectId"]))
            if ex is None:
                continue
            c["existingAbsent"] = bool(ex.get("isAbsent"))
            c["existing"] = None if c["existingAbsent"] else ex.get("score")
            same = (c["absent"] and c["existingAbsent"]) or (not c["absent"] and not c["existingAbsent"]
                    and c["existing"] is not None and abs(float(c["existing"]) - float(c["score"])) < 1e-9)
            if same:
                c["status"], c["msg"] = "exists_same", "Already saved with this value."
            elif c["status"] == "ready":
                c["status"], c["msg"] = "exists_diff", "A different mark is already saved."
            else:
                c["msg"] += " A different mark is already saved."
                c["hasExisting"] = True
    return {"columns": cols, "rows": out_rows}


def scan_parse_provider_json(text):
    """Extract and validate the JSON object a provider returned. Raises ValueError."""
    if not isinstance(text, str):
        raise ValueError("empty")
    t = text.strip()
    t = re.sub(r"^```(?:json)?|```$", "", t, flags=re.I | re.M).strip()
    start, end = t.find("{"), t.rfind("}")
    if start < 0 or end <= start:
        raise ValueError("no json")
    return json.loads(t[start:end + 1])


def scan_normalize_extraction(obj):
    """Turn provider output into the canonical structure sent to the browser.
    Hard limits and string sanitising: the model's output is treated as untrusted data."""
    if not isinstance(obj, dict):
        raise ValueError("bad shape")
    def clean(v, n=80):
        return re.sub(r"[\x00-\x1f]", " ", str(v if v is not None else "")).strip()[:n]
    def conf(v):
        f = to_float(v, 0.5)
        return round(min(max(f, 0.0), 1.0), 2)
    cols_in = obj.get("columns") or []
    if not isinstance(cols_in, list) or not cols_in:
        raise ValueError("no columns")
    columns = []
    for i, c in enumerate(cols_in[:60]):
        role = clean((c or {}).get("role"), 12).lower()
        columns.append({"index": i, "header": clean((c or {}).get("header"), 40),
                        "role": role if role in ("admission", "name", "subject", "total", "other") else "other"})
    rows = []
    for r in (obj.get("rows") or [])[:400]:
        cells_in = (r or {}).get("cells") or []
        if not isinstance(cells_in, list):
            continue
        cells = []
        for i in range(len(columns)):
            c = cells_in[i] if i < len(cells_in) and isinstance(cells_in[i], dict) else {}
            ill = bool(c.get("illegible"))
            cells.append({"text": "" if ill else clean(c.get("text"), 40), "conf": 0.0 if ill else conf(c.get("confidence")),
                          "illegible": ill, "raw": "" if ill else clean(c.get("raw", c.get("text")), 40),
                          "flag": clean(c.get("flag"), 140)})
        if any(c["text"] or c["illegible"] for c in cells):
            rows.append(cells)
    info = obj.get("examInfo") if isinstance(obj.get("examInfo"), dict) else {}
    return {"columns": columns, "rows": rows,
            "examInfo": {k: clean(info.get(k), 60) for k in ("name", "term", "year", "className", "maxMarks") if info.get(k)},
            "warnings": [clean(w, 160) for w in (obj.get("warnings") or [])[:5]] if isinstance(obj.get("warnings"), list) else []}


# ------------------------------------------------------------- OCR providers ----

class ScanProviderError(Exception):
    """Carries a message that is safe to show to teachers."""
    def __init__(self, public_message, admin_detail=None, retryable=True):
        super().__init__(public_message)
        self.public_message, self.admin_detail, self.retryable = public_message, admin_detail, retryable


def scan_provider_status():
    """-> dict describing the OCR engine. Never contacts a paid service; never exposes secrets."""
    vendor_ok = all(os.path.isfile(os.path.join(SCAN_OCR_VENDOR_DIR, f)) for f in SCAN_OCR_VENDOR_FILES)
    if SCAN_OCR_PROVIDER == "tesseract" and shutil.which("tesseract"):
        return {"provider": "server-tesseract", "mode": "server", "configured": True, "external": False,
                "label": "Free OCR on this server (Tesseract)", "setup": None, "selfHosted": vendor_ok,
                "privacy": "Photos are read on this server by the open-source Tesseract engine, then deleted straight away. "
                           "No outside service is used. Handwriting accuracy is low — check every value."}
    note = ""
    if SCAN_OCR_PROVIDER == "tesseract":
        note = " (SCAN_OCR_PROVIDER=tesseract is set but the tesseract program is not installed here, so the browser is used.)"
    return {"provider": "browser-tesseract", "mode": "browser", "configured": True, "external": False,
            "label": "Free OCR in your browser (Tesseract.js)", "setup": None, "selfHosted": vendor_ok,
            "privacy": "Photos are read inside this phone/computer and are not uploaded. Only the recognised text is sent to the "
                       "server for matching. Handwriting accuracy is limited — check every value." + note}


def scan_normalize_image(raw):
    """Validate bytes are a real JPEG/PNG/WEBP, strip metadata, fix rotation, downscale, re-encode as JPEG.
    Returns JPEG bytes. Raises ScanProviderError(retryable=False) for unusable files."""
    head = raw[:12]
    is_jpg = head[:3] == b"\xff\xd8\xff"
    is_png = head[:8] == b"\x89PNG\r\n\x1a\n"
    is_webp = head[:4] == b"RIFF" and head[8:12] == b"WEBP"
    if not (is_jpg or is_png or is_webp):
        raise ScanProviderError("That file is not a JPG, PNG or WEBP photo.", retryable=False)
    if not PIL_AVAILABLE:
        if is_jpg:
            return raw  # cannot re-encode without Pillow; the JPEG signature was verified
        raise ScanProviderError("The server needs the Pillow package to read this image type (pip install Pillow).", retryable=False)
    try:
        import io
        with Image.open(io.BytesIO(raw)) as im:
            im.load()
            im = ImageOps.exif_transpose(im)
            if im.mode not in ("RGB", "L"):
                im = im.convert("RGB")
            if max(im.size) > SCAN_MAX_DIM:
                im.thumbnail((SCAN_MAX_DIM, SCAN_MAX_DIM))
            buf = io.BytesIO()
            im.save(buf, "JPEG", quality=88)          # fresh encode: no EXIF / GPS survives
            return buf.getvalue()
    except ScanProviderError:
        raise
    except Exception:
        raise ScanProviderError("This photo could not be opened. Please retake it.", retryable=False)


# ---------------------------------------------------- free OCR -> table builder ----
# Turns OCR *word boxes* (from Tesseract.js in the browser, or the tesseract CLI on the
# server) into the canonical extraction structure. Pure functions: no DB, no network.
# Word format: {"t": text, "x0":, "y0":, "x1":, "y1":, "c": confidence 0-100}

_SCAN_NUM_FIX = {"O": "0", "o": "0", "D": "0", "l": "1", "I": "1", "|": "1", "i": "1",
                 "S": "5", "s": "5", "B": "8", "Z": "2", "z": "2", "G": "6"}
_SCAN_SKIP_TEXT = re.compile(r"^[\|\[\]\(\)_=~\\/•·:;,\.]+$")
_SCAN_FOOTER = re.compile(r"\b(TOTAL|MEAN|AVERAGE|AVG|SIGNATURE|SIGN|CLASS TEACHER|REMARKS|SUBJECT TEACHER|DATE)\b", re.I)
_SCAN_CLASS_RE = re.compile(r"\b(PP\s?[12]|GRADE\s?\d{1,2}|GR\.?\s?\d{1,2}|FORM\s?[1-6]|STD\.?\s?\d)\b", re.I)
_SCAN_EXAM_RE = re.compile(r"\b(OPENER|MID[\s\-]?TERM|END[\s\-]?(?:OF[\s\-]?)?TERM|CAT\s?\d?|ASSESSMENT\s?\d?|TEST\s?\d?)\b", re.I)


def _scan_clean_words(words):
    out = []
    for w in words or []:
        try:
            t = str(w.get("t", "")).strip()
            x0, y0, x1, y1 = (float(w[k]) for k in ("x0", "y0", "x1", "y1"))
            c = float(w.get("c", 0))
        except (TypeError, ValueError, KeyError, AttributeError):
            continue
        if not t or x1 <= x0 or y1 <= y0 or _SCAN_SKIP_TEXT.match(t):
            continue
        word = {"t": t[:60], "x0": x0, "y0": y0, "x1": x1, "y1": y1, "c": min(max(c, 0.0), 100.0),
                "xc": (x0 + x1) / 2.0, "yc": (y0 + y1) / 2.0, "h": y1 - y0}
        sy = w.get("sy")                         # optional [[char, confidence 0-100], ...] from the browser OCR
        if isinstance(sy, list) and sy and len(sy) <= 12:
            try:
                word["sy"] = [(str(a)[:1], min(max(float(b), 0.0), 100.0)) for a, b in sy]
            except (TypeError, ValueError):
                pass
        out.append(word)
    return out


def _scan_median(vals, default=10.0):
    v = sorted(vals)
    return v[len(v) // 2] if v else default


def _scan_group_lines(words):
    mh = _scan_median([w["h"] for w in words])
    lines = []
    for w in sorted(words, key=lambda w: w["yc"]):
        if lines and abs(w["yc"] - lines[-1]["y"]) <= 0.6 * mh:
            ln = lines[-1]
            ln["w"].append(w)
            ln["y"] = sum(x["yc"] for x in ln["w"]) / len(ln["w"])
        else:
            lines.append({"y": w["yc"], "w": [w]})
    for ln in lines:
        ln["w"].sort(key=lambda w: w["x0"])
    return lines, mh


def _scan_split_cells(line_words, mh, gap_factor=1.0, split_roles=False, hint_set=frozenset()):
    """Group a line's words into cells by horizontal gap. For header lines (split_roles=True) also
    start a new cell at a word that is clearly its own heading (NAME, TOTAL, a known subject)."""
    cells, cur = [], [line_words[0]]
    for w in line_words[1:]:
        own = split_roles and (_scan_header_role(w["t"]) in ("name", "total")
                               or scan_norm_header(w["t"]) in hint_set)
        if own or w["x0"] - cur[-1]["x1"] > mh * gap_factor:
            cells.append(cur)
            cur = [w]
        else:
            cur.append(w)
    cells.append(cur)
    return [{"text": " ".join(w["t"] for w in c), "x0": c[0]["x0"], "x1": c[-1]["x1"],
             "xc": (c[0]["x0"] + c[-1]["x1"]) / 2.0, "conf": min(w["c"] for w in c) / 100.0} for c in cells]


def _scan_header_role(text):
    u = re.sub(r"[^A-Z0-9%/]", "", text.upper())
    if not u:
        return "other"
    if re.search(r"ADM|REG", u) or u in ("ADMISSION", "ADMNO"):
        return "admission"
    if re.search(r"NAME", u) or u in ("STUDENT", "PUPIL", "LEARNER"):
        return "name"
    if u in ("GEN", "GENDER", "SEX", "MF", "M/F", "GND"):
        return "other"                      # gender, not a mark: never imported
    if u in ("PTS", "PNTS", "PT", "PTS."):
        return "total"                      # points column: never a subject mark
    if re.search(r"TOTAL|MEAN|POS|RANK|GRADE|AVG|AVERAGE|POINTS|PERCENT|%|REMARK|SIGN", u):
        return "total"
    if u in ("SN", "S/N", "S/NO", "SNO", "#", "NO", "NUMBER", "NUM", "SERIAL", "SL"):
        return "other"
    return "subject"


def _scan_hint_set(hints):
    s = set()
    for h in hints or []:
        k = scan_norm_header(h)
        if k:
            s.add(k)
    return s


def _scan_find_header(lines, mh, hint_set):
    best, best_score = None, 0.0
    for idx, ln in enumerate(lines[:15]):
        cells = _scan_split_cells(ln["w"], mh, split_roles=True, hint_set=hint_set)
        if len(cells) < 3:
            continue
        score = 0.0
        for c in cells:
            role = _scan_header_role(c["text"])
            if role in ("admission", "name", "total"):
                score += 2
            elif scan_norm_header(c["text"]) in hint_set:
                score += 2
            elif re.fullmatch(r"[A-Za-z][A-Za-z.\- ]{1,14}", c["text"]):
                score += 0.5
        digit_cells = sum(1 for c in cells if re.fullmatch(r"[\d.\s]+", c["text"]))
        score -= digit_cells * 1.5
        if score > best_score:
            best, best_score = (idx, cells), score
    if best and best_score >= 3:
        return best
    return None


def _scan_exam_info(lines):
    text = " | ".join(" ".join(w["t"] for w in ln["w"]) for ln in lines)
    info = {}
    m = re.search(r"\bTERM\s*([123])\b", text, re.I)
    if m:
        info["term"] = f"Term {m.group(1)}"
    m = re.search(r"\b(20\d\d)\b", text)
    if m:
        info["year"] = m.group(1)
    m = _SCAN_CLASS_RE.search(text)
    if m:
        info["className"] = re.sub(r"\s+", " ", m.group(1)).strip()
    m = _SCAN_EXAM_RE.search(text)
    if m:
        info["name"] = re.sub(r"\s+", " ", m.group(1)).strip().title()
    m = re.search(r"(?:OUT\s+OF|MAX(?:IMUM)?(?:\s+MARKS?)?)\s*[:=]?\s*(\d{2,3})\b", text, re.I)
    if m:
        info["maxMarks"] = m.group(1)
    return info


def _scan_fix_numeric(token):
    """Fix common OCR letter/digit confusions in a number cell. Returns (text, changed)."""
    if not re.search(r"\d", token):
        return token, False
    out, changed = [], False
    for ch in token:
        if ch.isdigit() or ch in ".,-":
            out.append(ch)
        elif ch in _SCAN_NUM_FIX:
            out.append(_SCAN_NUM_FIX[ch]); changed = True
        else:
            out.append(ch)
    return "".join(out), changed


_SCAN_CODE_RE = re.compile(r"(?i)^(?:(?:EE|ME|AE|BE)\s?[12IlL|]?|[A-Za-z]{2}[0-9IlL|])$")


def _scan_is_code_word(t):
    """A short performance-level code such as EE1 / ME2 / AE2 / BE1 (OCR may misread the digit)."""
    return bool(_SCAN_CODE_RE.match(str(t or "").strip()))


def _scan_merge_codes(ws, mh):
    """Join a number and the performance code written right after it ('76' + 'EE2') into ONE box so the
    pair always lands in the same column and can never be split across neighbouring columns."""
    out = []
    for w in ws:
        prev = out[-1] if out else None
        if (prev is not None and not prev.get("merged") and _scan_is_code_word(w["t"])
                and re.search(r"[0-9]", prev["t"]) and len(re.findall(r"[A-Za-z]", prev["t"])) < 2
                and 0 <= w["x0"] - prev["x1"] <= 2.0 * mh):
            m = dict(prev)
            m.update(t=prev["t"] + " " + w["t"], x1=w["x1"], xc=(prev["x0"] + w["x1"]) / 2.0, merged=True)
            out[-1] = m            # confidence and per-digit confidences stay those of the NUMBER, not the code
        else:
            out.append(w)
    return out


def _scan_number_conf(cell_words):
    """Confidence (0-1) of the digits of the mark only; the performance code does not count.
    Uses per-digit confidences when the OCR supplied them. Returns None if nothing is known."""
    vals = []
    for w in cell_words:
        if _scan_is_code_word(w["t"]) and not re.search(r"[0-9]", w["t"][:1]):
            continue
        vals.append(w["c"])
        for ch, cf in (w.get("sy") or []):
            if ch.isdigit():
                vals.append(cf)
    return (min(vals) / 100.0) if vals else None


def scan_words_to_extraction(words, hints=None):
    """Word boxes -> provider-style dict ({columns, rows, examInfo, warnings}) for scan_normalize_extraction.
    Raises ValueError if no table-like structure can be found."""
    words = _scan_clean_words(words)
    if len(words) < 6:
        raise ValueError("too few words")
    lines, mh = _scan_group_lines(words)
    found = _scan_find_header(lines, mh, _scan_hint_set(hints))
    warnings = []
    if not found:
        raise ValueError("no header")
    hidx, hcells = found
    columns = [{"header": c["text"][:40], "role": _scan_header_role(c["text"]), "x0": c["x0"], "x1": c["x1"], "xc": c["xc"]}
               for c in hcells]
    if not any(c["role"] == "admission" for c in columns):          # a bare "No." column is the admission number
        for c in columns:
            if re.sub(r"[^A-Z]", "", c["header"].upper()) in ("NO", "NUMBER", "NUM"):
                c["role"] = "admission"; break
    name_cols = [i for i, c in enumerate(columns) if c["role"] == "name"]
    subj_cols = [i for i, c in enumerate(columns) if c["role"] == "subject"]
    if not subj_cols:
        raise ValueError("no subject columns")
    first_numeric_x0 = min(columns[i]["x0"] for i in range(len(columns)) if columns[i]["role"] in ("subject", "total"))

    def dist(w, c):
        d = 0.0 if c["x0"] <= w["xc"] <= c["x1"] else min(abs(w["xc"] - c["x0"]), abs(w["xc"] - c["x1"]))
        return (d, abs(w["xc"] - c["xc"]))

    rows = []
    num_x0 = first_numeric_x0 - 0.5 * mh
    for ln in lines[hidx + 1:]:
        ws = _scan_merge_codes(ln["w"], mh)
        cells = [[] for _ in columns]
        for w in ws:
            alpha = len(re.findall(r"[A-Za-z]", w["t"])) >= 2 and not re.fullmatch(r"(?i)ab|abs|absent", w["t"])
            if alpha and (re.match(r"^[0-9]", w["t"]) or (_scan_is_code_word(w["t"]) and w["xc"] >= num_x0)):
                alpha = False                         # "76EE2" / a lone "EE2" in the marks area belongs to a marks column
            if alpha and name_cols and w["xc"] < num_x0:
                cells[name_cols[0]].append(w)
            else:
                cands = [i for i, c in enumerate(columns) if not (alpha and c["role"] in ("subject", "total", "admission"))] or \
                        list(range(len(columns)))
                cells[min(cands, key=lambda i: dist(w, columns[i]))].append(w)
        flat = " ".join(w["t"] for cw in cells for w in cw)
        row_cells, any_content = [], False
        for i, cw in enumerate(cells):
            cw.sort(key=lambda w: w["x0"])
            text = " ".join(w["t"] for w in cw).strip()
            raw, flag = text, ""
            conf = (min(w["c"] for w in cw) / 100.0) if cw else 0.0
            role = columns[i]["role"]
            if text and role == "admission":
                text, changed = _scan_fix_numeric(text)
                if changed:
                    conf = min(conf, 0.6)           # a substitution was made -> force the teacher to check it
            elif text and role == "subject":
                # Keep ONLY the numeric mark. The performance code (EE1, ME2 ...) is dropped; the original OCR
                # text stays available in `raw` so the teacher can compare.
                sp = scan_split_mark(text)
                if sp["kind"] == "number" and not sp["multi"]:
                    text = sp["num"]
                    nconf = _scan_number_conf(cw)
                    conf = nconf if nconf is not None else conf
                    if sp["substituted"]:
                        conf, flag = min(conf, 0.6), "A letter was read as a digit - check the mark against the paper."
                    elif nconf is not None and nconf < 0.7:
                        flag = "A digit was read with low certainty - check it against the paper."
                elif sp["kind"] == "dash":
                    text = "-"
                elif sp["kind"] == "absent":
                    text = "AB"
            if role == "name":
                text = re.sub(r"[^A-Za-z'\-\. ]", "", text).strip()
            conf = round(min(conf, 0.97), 2)
            if text:
                any_content = True
            row_cells.append({"text": text, "raw": raw[:40], "flag": flag, "confidence": conf if text else 0.0})
        if not any_content:
            continue
        adm_i = next((i for i, c in enumerate(columns) if c["role"] == "admission"), None)
        adm_txt = row_cells[adm_i]["text"] if adm_i is not None else ""
        name_txt = " ".join(row_cells[i]["text"] for i in name_cols)
        if _SCAN_FOOTER.search(flat) and not re.search(r"\d", adm_txt) and not name_txt:
            continue
        marks = sum(1 for i in subj_cols if row_cells[i]["text"])
        if not adm_txt and not name_txt and marks < 2:
            continue                                  # stray text line, not a pupil row
        rows.append({"cells": row_cells})
    if not rows:
        raise ValueError("no rows")
    warnings.append("Free OCR was used. Handwriting is often misread — check every value against the paper.")
    return {"examInfo": _scan_exam_info(lines[:hidx]),
            "columns": [{"header": c["header"], "role": c["role"]} for c in columns],
            "rows": rows, "warnings": warnings}


def scan_tsv_to_words(tsv_text):
    """tesseract `tsv` output -> word boxes."""
    words = []
    for ln in (tsv_text or "").splitlines()[1:]:
        p = ln.split("\t")
        if len(p) < 12 or not p[11].strip():
            continue
        try:
            x, y, w, h, c = int(p[6]), int(p[7]), int(p[8]), int(p[9]), float(p[10])
        except ValueError:
            continue
        if c < 0:
            continue
        words.append({"t": p[11].strip(), "x0": x, "y0": y, "x1": x + w, "y1": y + h, "c": c})
    return words


def _scan_call_tesseract(image_path, hints=None):
    """Server-side free OCR. Reads word boxes with the tesseract CLI and builds the table. Returns JSON text."""
    try:
        proc = subprocess.run(["tesseract", image_path, "stdout", "--psm", "6", "tsv"],
                              capture_output=True, text=True, timeout=SCAN_OCR_TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ScanProviderError("Reading this photo took too long and was stopped. Try again.")
    except OSError:
        raise ScanProviderError("Server OCR is not installed.", retryable=False)
    if proc.returncode != 0:
        raise ScanProviderError("Local OCR could not read this photo.")
    try:
        return json.dumps(scan_words_to_extraction(scan_tsv_to_words(proc.stdout), hints))
    except ValueError:
        raise ScanProviderError("No marks table could be found in this photo. Retake it flat, well lit and fully in frame, "
                                "or use manual entry.")


SCAN_PROVIDERS = {"server-tesseract": _scan_call_tesseract}


def scan_process_image_bytes(raw, hints=None):
    """Server-OCR pipeline (only used when SCAN_OCR_PROVIDER=tesseract). Returns (extraction_dict, cleanup_info).
    The temp file is always deleted in `finally`, success or failure."""
    status = scan_provider_status()
    if status["provider"] not in SCAN_PROVIDERS:
        raise ScanProviderError("Photos are read in your browser on this server; server-side reading is not enabled.", retryable=False)
    jpeg = scan_normalize_image(raw)
    _scan_ensure_tmp_dir()
    fd, path = tempfile.mkstemp(prefix="scan_", suffix=".jpg", dir=SCAN_TMP_DIR)   # random name, mode 0600
    with _scan_lock:
        _scan_live_files.add(path)
    cleanup = {"deleted": False}
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(jpeg)
        del jpeg
        text = SCAN_PROVIDERS[status["provider"]](path, hints)
        try:
            extraction = scan_normalize_extraction(scan_parse_provider_json(text))
        except (ValueError, json.JSONDecodeError):
            raise ScanProviderError("The photo was read but no marks table could be recognised. Retake it flat, well lit and fully in frame.")
        return extraction, cleanup
    finally:
        cleanup["deleted"] = scan_secure_delete(path)
        if not cleanup["deleted"]:
            log.error("Marksheet temp file could not be deleted; janitor will retry.")


# ----------------------------------------------------------- DB-facing helpers ----

def scan_user_can(u, class_id, subject_id):
    """Server-side class/subject authorisation for scanning/importing."""
    if not role_has_module(u.role, "marks"):
        return False
    if u.role in PRIVILEGED_STAFF_ROLES or not SCAN_RESTRICT_TEACHERS:
        return True
    if u.role in TEACHER_ROLES:
        if not teacher_can_access_class(u, class_id):
            return False
        return subject_id is None or teacher_can_access_subject(u, subject_id, class_id)
    return False


def _scan_exam_for(u, class_id, exam_id):
    """-> (exam_data, error). Exam must exist and cover the class (mirrors the Marks Entry exam dropdown)."""
    exam = _record_data("exams", exam_id)
    if not exam:
        return None, "Unknown examination."
    ids = exam.get("classIds") or ([exam["classId"]] if exam.get("classId") else [])
    if class_id not in ids:
        return None, "That examination is not set for the selected class."
    if not _record_data("classes", class_id):
        return None, "Unknown class."
    return exam, None


def _scan_load_ctx(u, class_id, exam_id):
    exam, err = _scan_exam_for(u, class_id, exam_id)
    if err:
        return None, err
    s = db()
    settings = scan_get_settings()
    students = {r.id: dict(r.data or {}, id=r.id) for r in s.query(Record).filter_by(store="students", deleted=False).all()}
    subjects = [dict(r.data or {}, id=r.id) for r in s.query(Record).filter_by(store="subjects", deleted=False).all()]
    existing = {}
    for r in s.query(Record).filter_by(store="marks", deleted=False, exam_id=exam_id).all():
        d = r.data or {}
        if d.get("studentId") and d.get("subjectId"):
            existing[(d["studentId"], d["subjectId"])] = d
    return {"width": settings["admissionWidth"], "threshold": settings["confidenceThreshold"], "exam": exam,
            "class_id": class_id, "students": students, "subjects": subjects, "abbreviations": settings["abbreviations"],
            "existing": existing, "can_subject": (lambda sid: scan_user_can(u, class_id, sid)),
            "privileged": u.role in PRIVILEGED_STAFF_ROLES}, None


# ------------------------------------------------------------------- routes ----

def _scan_hints():
    """Subject names/codes/saved abbreviations, used only to recognise header cells."""
    out = []
    for r in db().query(Record).filter_by(store="subjects", deleted=False).all():
        d = r.data or {}
        out += [d.get("name"), d.get("code")]
    out += [a.get("abbr") for a in scan_get_settings().get("abbreviations", [])]
    return [x for x in out if x]


@app.route("/api/marksheet-scan/config", methods=["GET"])
@login_required
@require_module("marks")
def scan_config():
    u = current_user()
    st = scan_provider_status()
    settings = scan_get_settings()
    out = {"configured": True, "provider": st["provider"], "mode": st["mode"], "providerLabel": st["label"],
           "external": False, "privacy": st["privacy"], "setup": None, "selfHosted": st["selfHosted"],
           "maxImages": SCAN_MAX_IMAGES, "maxImageBytes": SCAN_MAX_IMAGE_BYTES, "settings": settings,
           "canEditSettings": u.role in PRIVILEGED_STAFF_ROLES}
    return jsonify(out)


@app.route("/vendor/tesseract/<path:filename>", methods=["GET"])
def scan_vendor_asset(filename):
    """Optional self-hosted Tesseract.js files. Served explicitly so the .gz language file is sent as raw bytes
    (the generic static route would add Content-Encoding: gzip and the browser would unzip it before Tesseract.js can)."""
    if filename.startswith(".") or ".." in filename:
        abort(404)
    full = os.path.join(SCAN_OCR_VENDOR_DIR, filename)
    if not os.path.isfile(full):
        abort(404)
    mime = "application/octet-stream" if filename.endswith(".gz") else None
    resp = send_from_directory(SCAN_OCR_VENDOR_DIR, filename, mimetype=mime, max_age=86400 * 30)
    if filename.endswith(".gz"):
        resp.headers.pop("Content-Encoding", None)
    return resp


@app.route("/api/marksheet-scan/settings", methods=["PUT"])
@login_required
@csrf_protect
@require_roles(*PRIVILEGED_STAFF_ROLES)
def scan_save_settings():
    u = current_user()
    subject_ids = {r.id for r in db().query(Record).filter_by(store="subjects", deleted=False).all()}
    clean, err = scan_validate_settings(request.get_json(silent=True) or {}, subject_ids)
    if err:
        return json_error(err, 400)
    rec = db().query(Record).filter_by(store="settings", id=SCAN_SETTINGS_ID).first()
    if rec:
        rec.data = {**clean, "id": SCAN_SETTINGS_ID}
        rec.deleted = False
        rec.rev = (rec.rev or 1) + 1
        rec.updated_by = u.username
    else:
        db().add(Record(id=SCAN_SETTINGS_ID, store="settings", data={**clean, "id": SCAN_SETTINGS_ID}, rev=1, updated_by=u.username))
    db().commit()
    log_activity(f"{u.name} updated Marksheet Scanning settings", u)
    return jsonify({"settings": clean})


@app.route("/api/marksheet-scan/process", methods=["POST"])
@login_required
@csrf_protect
@require_module("marks")
def scan_process():
    """Body = the raw image bytes (Content-Type image/*). Query: classId, examId."""
    u = current_user()
    class_id, exam_id = request.args.get("classId", ""), request.args.get("examId", "")
    if not scan_user_can(u, class_id, None):
        return json_error("You are not allowed to scan marks for this class.", 403)
    _exam, err = _scan_exam_for(u, class_id, exam_id)
    if err:
        return json_error(err, 400)
    if request.content_length is not None and request.content_length > SCAN_MAX_IMAGE_BYTES:
        return json_error(f"This photo is too large (limit {SCAN_MAX_IMAGE_BYTES // (1024*1024)} MB). It will be shrunk automatically — try again.", 413)
    raw = request.get_data(cache=False)
    if not raw:
        return json_error("No photo was received. Please try again.", 400)
    if len(raw) > SCAN_MAX_IMAGE_BYTES:
        return json_error("This photo is too large.", 413)
    try:
        extraction, cleanup = scan_process_image_bytes(raw, _scan_hints())
    except ScanProviderError as exc:
        resp = {"error": exc.public_message, "retryable": exc.retryable, "imagesDeleted": True}
        if exc.admin_detail and u.role in ADMIN_ROLES:
            resp["detail"] = exc.admin_detail
        status = 503 if "set up" in exc.public_message.lower() or "SCAN_" in exc.public_message else 422
        return jsonify(resp), status
    except Exception as exc:  # noqa: BLE001
        safe_log_error("scan_process failed", exc)
        return jsonify({"error": "Something went wrong while reading this photo. Please retry.", "retryable": True}), 500
    finally:
        del raw
    return jsonify({"extraction": extraction, "imagesDeleted": cleanup["deleted"],
                    "message": ("Analysis complete. Uploaded image has been deleted." if cleanup["deleted"] else
                                "Analysis complete, but the temporary copy could not be removed immediately; it will be purged automatically within minutes.")})


@app.route("/api/marksheet-scan/structure", methods=["POST"])
@login_required
@csrf_protect
@require_module("marks")
def scan_structure():
    """Browser OCR path. Body: {classId, examId, words:[{t,x0,y0,x1,y1,c}]} — recognised text boxes only, never an image.
    Returns the same {extraction} shape as /process. Nothing is saved."""
    u = current_user()
    payload = request.get_json(silent=True) or {}
    class_id, exam_id = payload.get("classId", ""), payload.get("examId", "")
    if not scan_user_can(u, class_id, None):
        return json_error("You are not allowed to scan marks for this class.", 403)
    _exam, err = _scan_exam_for(u, class_id, exam_id)
    if err:
        return json_error(err, 400)
    words = payload.get("words")
    if not isinstance(words, list) or not words:
        return jsonify({"error": "No text was recognised in this photo. Retake it flat, well lit and fully in frame, or use manual entry.",
                        "retryable": True}), 422
    if len(words) > 8000:
        return json_error("This page has too much text to read at once. Crop it or split it into two photos.", 413)
    try:
        extraction = scan_normalize_extraction(scan_words_to_extraction(words, _scan_hints()))
    except ValueError:
        return jsonify({"error": "Text was read but no marks table could be found. Make sure the column headings are in the photo "
                                 "(crop to the table), or use manual entry.", "retryable": True}), 422
    except Exception as exc:  # noqa: BLE001
        safe_log_error("scan_structure failed", exc)
        return jsonify({"error": "Something went wrong while building the table. Please retry or use manual entry.", "retryable": True}), 500
    return jsonify({"extraction": extraction, "imagesDeleted": True,
                    "message": "Text recognised on your device. No photo was uploaded."})


@app.route("/api/marksheet-scan/admission-audit", methods=["GET"])
@login_required
@require_roles(*PRIVILEGED_STAFF_ROLES)
def scan_admission_audit():
    """READ-ONLY compatibility report. Lists students whose stored admission number is not already the school's
    four-digit string, and groups that would collide once padded. It changes NOTHING; the scanner already
    matches such older numbers through their padded form, so running a migration is optional (see SCAN_SETUP.md)."""
    width = scan_get_settings()["admissionWidth"]
    students = [dict(r.data or {}, id=r.id) for r in db().query(Record).filter_by(store="students", deleted=False).all()]
    needs_padding, unusable, groups = [], [], {}
    for st in students:
        stored = str(st.get("admissionNo") or "").strip()
        label = {"id": st["id"], "name": f"{st.get('firstName','')} {st.get('lastName','')}".strip(),
                 "classId": st.get("classId"), "stored": stored}
        if not stored:
            unusable.append({**label, "problem": "no admission number"}); continue
        norm, status = scan_normalize_admission(stored, width)
        if status != "ok":
            unusable.append({**label, "problem": f"cannot be turned into a {width}-digit number"}); continue
        groups.setdefault(norm, []).append(label)
        if norm != stored:
            needs_padding.append({**label, "wouldBecome": norm})
    collisions = {k: v for k, v in groups.items() if len(v) > 1}
    return jsonify({"width": width, "students": len(students), "alreadyCorrect": len(students) - len(needs_padding) - len(unusable),
                    "needsPadding": needs_padding[:500], "needsPaddingCount": len(needs_padding),
                    "unusable": unusable[:500], "collisions": collisions,
                    "safeToMigrate": not collisions and not unusable})


@app.route("/api/marksheet-scan/match", methods=["POST"])
@login_required
@csrf_protect
@require_module("marks")
def scan_match():
    u = current_user()
    payload = request.get_json(silent=True) or {}
    class_id, exam_id = payload.get("classId", ""), payload.get("examId", "")
    if not scan_user_can(u, class_id, None):
        return json_error("You are not allowed to work with this class.", 403)
    if len(payload.get("rows") or []) > 1500 or len(payload.get("columns") or []) > 200:
        return json_error("Too much data in one session.", 413)
    ctx, err = _scan_load_ctx(u, class_id, exam_id)
    if err:
        return json_error(err, 400)
    result = scan_match_rows(payload, ctx)
    result["exam"] = {"id": exam_id, "name": ctx["exam"].get("name"), "max": ctx["exam"].get("maxMarks") or 100}
    return jsonify(result)


@app.route("/api/marksheet-scan/import", methods=["POST"])
@login_required
@csrf_protect
@require_module("marks")
def scan_import():
    u = current_user()
    payload = request.get_json(silent=True) or {}
    class_id, exam_id = payload.get("classId", ""), payload.get("examId", "")
    token = str(payload.get("importToken") or "")[:64]
    items = payload.get("items") or []
    if not isinstance(items, list) or not items or len(items) > 5000:
        return json_error("There are no marks to import.", 400)
    if not scan_user_can(u, class_id, None):
        return json_error("You are not allowed to import marks for this class.", 403)
    # Double-click / retry guard: a token that already completed returns its original result.
    if token:
        with _scan_lock:
            for k in [k for k, v in _scan_import_cache.items() if time.time() - v[0] > 3600]:
                _scan_import_cache.pop(k, None)
            hit = _scan_import_cache.get(token)
        if hit and hit[1] is not None:
            return jsonify({**hit[1], "duplicateRequest": True})
        if hit and hit[1] is None:
            return json_error("This import is already running. Please wait.", 409)
        with _scan_lock:
            _scan_import_cache[token] = (time.time(), None)
    try:
        result = _scan_do_import(u, class_id, exam_id, items)
    except Exception as exc:  # noqa: BLE001
        db().rollback()
        safe_log_error("scan_import failed", exc)
        if token:
            with _scan_lock:
                _scan_import_cache.pop(token, None)      # allow a safe retry
        return json_error("The import failed and nothing was changed. Your corrections are still on screen — please retry.", 500)
    if isinstance(result, tuple):                         # (response, status) validation error
        if token:
            with _scan_lock:
                _scan_import_cache.pop(token, None)
        return result
    if token:
        with _scan_lock:
            _scan_import_cache[token] = (time.time(), result)
    return jsonify(result)


def _scan_do_import(u, class_id, exam_id, items):
    ctx, err = _scan_load_ctx(u, class_id, exam_id)
    if err:
        return json_error(err, 400)
    s = db()
    exam_max = to_float(ctx["exam"].get("maxMarks"), 100) or 100
    limits = {a["subjectId"]: a["maxMarks"] for a in ctx["abbreviations"] if a.get("maxMarks")}
    subj_ids = {x["id"] for x in ctx["subjects"]}
    seen, skipped, saved_ids, created, updated, unchanged = set(), [], [], 0, 0, 0
    for it in items:
        sid, subj = it.get("studentId"), it.get("subjectId")
        ref = {"studentId": sid, "subjectId": subj}
        stu = ctx["students"].get(sid)
        if not stu or stu.get("classId") != class_id or stu.get("status") in ("Archived", "Transferred"):
            skipped.append({**ref, "reason": "Student is not in the selected class."}); continue
        if subj not in subj_ids:
            skipped.append({**ref, "reason": "Unknown subject."}); continue
        if not scan_user_can(u, class_id, subj):
            skipped.append({**ref, "reason": "You are not assigned to this subject."}); continue
        if (sid, subj) in seen:
            skipped.append({**ref, "reason": "Duplicate entry in the same import."}); continue
        seen.add((sid, subj))
        is_abs = bool(it.get("isAbsent"))
        score = None
        if not is_abs:
            raw = it.get("score")
            try:
                score = float(raw)
            except (TypeError, ValueError):
                skipped.append({**ref, "reason": "Mark is not a number."}); continue
            if not (0 <= score <= min(exam_max, limits.get(subj, exam_max))):
                skipped.append({**ref, "reason": "Mark is outside the allowed range."}); continue
        ex_rec = s.query(Record).filter_by(store="marks", deleted=False, exam_id=exam_id, student_id=sid, subject_id=subj).first()
        if ex_rec:
            ed = ex_rec.data or {}
            same = (bool(ed.get("isAbsent")) == is_abs) and (is_abs or (ed.get("score") is not None and abs(float(ed["score"]) - score) < 1e-9))
            if same:
                unchanged += 1; saved_ids.append(ex_rec.id); continue
            if not it.get("replace"):
                skipped.append({**ref, "reason": "A different mark already exists and replacing was not approved."}); continue
        data = {"examId": exam_id, "studentId": sid, "subjectId": subj, "maxMarks": exam_max, "score": score, "isAbsent": is_abs}
        if ex_rec:
            data["id"] = ex_rec.id
            ex_rec.data = data
            ex_rec.rev = (ex_rec.rev or 1) + 1
            ex_rec.updated_by = u.username
            for k, v in _extract_indexed_fields(data).items():
                setattr(ex_rec, k, v)
            updated += 1; saved_ids.append(ex_rec.id)
        else:
            rid = new_id("mk")
            data["id"] = rid
            s.add(Record(id=rid, store="marks", data=data, rev=1, updated_by=u.username, **_extract_indexed_fields(data)))
            created += 1; saved_ids.append(rid)
    s.commit()                                              # one transaction: all valid rows or none
    records = [r.to_dict() for r in s.query(Record).filter(Record.store == "marks", Record.id.in_(saved_ids)).all()] if saved_ids else []
    log_activity(f"{u.name} imported {created + updated} mark(s) from a scanned marksheet for exam {exam_id}", u)
    return {"created": created, "updated": updated, "unchanged": unchanged, "skipped": skipped, "records": records}



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
    "reportRemarks": "report-remarks",
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
        vis = teacher_visible_class_ids(u) if (store == "students" and u.role in TEACHER_ROLES) else None
        stores[store] = [serialize_for_user(u, store, r, vis) for r in rows]
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
        vis = teacher_visible_class_ids(u) if (store == "students" and u.role in TEACHER_ROLES) else None
        result[store] = [serialize_for_user(u, store, r, vis) for r in rows]
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
    scan_startup()               # private temp dir + janitor for marksheet-scan images
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
