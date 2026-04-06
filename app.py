import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# 1. Initialize Firebase Admin SDK AT THE VERY TOP
load_dotenv()
firebase_key_raw = os.getenv('FIREBASE_KEY')
service_account_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'serviceAccountKey.json')

if not firebase_admin._apps:
    try:
        if firebase_key_raw:
            # Parse the JSON string from environment variable
            cred_dict = json.loads(firebase_key_raw)
            project_id = cred_dict.get('project_id')
            cred = credentials.Certificate(cred_dict)
            firebase_admin.initialize_app(cred, {
                'projectId': project_id
            })
            print(f"Firebase initialized successfully for project: {project_id}")
        elif os.path.isfile(service_account_path):
            cred = credentials.Certificate(service_account_path)
            firebase_admin.initialize_app(cred)
            print(f"Firebase initialized successfully from local service account file: {service_account_path}")
        else:
            # Fallback for local development if no explicit credentials are provided
            firebase_admin.initialize_app()
            print("Firebase initialized with default credentials")
    except Exception as e:
        print(f"CRITICAL: Firebase initialization failed: {e}")

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, session, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import pandas as pd
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Student, Subject, Attendance, Classroom, Department, get_db
from datetime import datetime
from functools import wraps
import csv
import io

def admin_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'hod']:
            flash('Unauthorized Access! HOD/Admin only.', 'danger')
            return redirect(url_for('dashboard'))
        return f(*args, **kwargs)
    return decorated_function

def teacher_allowed(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role not in ['admin', 'teacher', 'in_charge', 'hod']:
            flash('Unauthorized Access! Staff only.', 'danger')
            return redirect(url_for('student_dashboard' if current_user.role == 'student' else 'login'))
        return f(*args, **kwargs)
    return decorated_function

def security_required(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated or current_user.role != 'security':
            flash('Unauthorized Access! Security only.', 'danger')
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function

import secrets

app = Flask(__name__)
app.config['SECRET_KEY'] = os.environ.get("SECRET_KEY", "dev")
# supabase or local sqlite settings removed for firebase


db.init_app(app)

login_manager = LoginManager()
login_manager.login_view = 'login'
login_manager.init_app(app)

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(user_id)


def create_default_admin():
    try:
        if not User.query.filter_by(role='admin').first():
            hashed_pw = generate_password_hash('admin123', method='pbkdf2:sha256')
            admin = User(
                name='Super Admin',
                email='admin@gmail.com',
                username='admin@gmail.com',
                password=hashed_pw,
                role='admin'
            )
            admin.save()
            print("Default admin created.")
    except Exception as e:
        print(f"Error creating default admin: {e}")

# Create default admin safely
with app.app_context():
    create_default_admin()
    
    # ensure all stored usernames/emails are normalized to lowercase
    try:
        all_users = User.query.all()
        for u in all_users:
            changed = False
            if hasattr(u, 'email') and u.email:
                normalized = u.email.strip().lower()
                if u.email != normalized:
                    u.email = normalized
                    changed = True
            if hasattr(u, 'username') and u.username:
                normalized = u.username.strip().lower()
                if u.username != normalized:
                    u.username = normalized
                    changed = True
            if changed:
                u.save()
        # note: this will update any existing teacher records too
    except Exception as e:
        print(f"Error normalizing users: {e}")

# --- Performance Caching ---
_cache = {
    'departments': {'data': None, 'time': None},
    'classrooms': {'data': None, 'time': None},
    'subjects': {'data': None, 'time': None}
}
CACHE_TIMEOUT = 300 # 5 minutes

def get_cached_metadata(key, model_class):
    now = datetime.now().timestamp()
    if _cache[key]['data'] is None or (now - _cache[key]['time']) > CACHE_TIMEOUT:
        data = model_class.query.all()
        # Sort if needed
        if key == 'departments': data.sort(key=lambda x: x.name.lower())
        elif key == 'classrooms': data.sort(key=lambda x: x.name.lower())
        _cache[key] = {'data': data, 'time': now}
    return _cache[key]['data']

# --- Routes ---
@app.route('/health')
def health():
    return "OK", 200


@app.route('/')
@login_required
def dashboard():
    if current_user.role == 'student':
        return redirect(url_for('student_dashboard'))
    
    # Cache expensive counts
    assigned_ids = [str(x) for x in getattr(current_user, 'assigned_classes', []) if x]
    
    if current_user.role in ['teacher', 'in_charge']:
        # Teacher Specific Counts
        total_students = Student.query.where('class_id', 'in', assigned_ids).count() if assigned_ids else 0
        total_subjects = Subject.query.count() # Subjects might be common, keep for now or filter if needed
        total_classes = len(assigned_ids)
        total_teachers = User.query.where('role', 'in', ['teacher', 'in_charge', 'hod']).count()
    else:
        # Admin / HOD Full View
        if 'global_counts' not in _cache or (datetime.now().timestamp() - _cache['global_counts']['time']) > CACHE_TIMEOUT:
            _cache['global_counts'] = {
                'data': {
                    'students': Student.query.count(),
                    'subjects': Subject.query.count(),
                    'classes': Classroom.query.count(),
                    'teachers': User.query.where('role', 'in', ['teacher', 'in_charge', 'hod']).count()
                },
                'time': datetime.now().timestamp()
            }
        
        counts = _cache['global_counts']['data']
        total_students = counts['students']
        total_subjects = counts['subjects']
        total_classes = counts['classes']
        total_teachers = counts['teachers']

    
    # Optimized Top Students Summary
    if current_user.role in ['teacher', 'in_charge'] and assigned_ids:
        # Fetch students in assigned classes
        top_students = Student.query.where('class_id', 'in', assigned_ids).limit(10).all()
    else:
        top_students = Student.query.limit(10).all()

    report_data = []
    for s in top_students:
        # We only calculate stats for these 10 students now, much faster than calculating for everyone in pool
        stats = s.get_attendance_stats()
        report_data.append({
            'name': getattr(s, 'name', ''),
            'roll_no': getattr(s, 'roll_no', ''),
            'dept': getattr(s, 'dept', ''),
            'perc': stats[2]
        })

    
    today = datetime.utcnow().date()
    today_str = today.strftime('%Y-%m-%d')
    in_charge_data = None
    hod_summary = []

    attendance_today = Attendance.query.filter_by(date=today_str).all()
    student_today_statuses = {}
    for rec in attendance_today:
        sid = str(getattr(rec, 'student_id', ''))
        if not sid: continue
        if sid not in student_today_statuses:
            student_today_statuses[sid] = set()
        student_today_statuses[sid].add(getattr(rec, 'status', ''))
        
    # Resolve student depts and classes for stats - ONLY for students marked today
    # To be robust (handling doc ids, roll numbers, and Firestore internal ID mapping), 
    # we'll fetch all students once and map them. This is efficient for small-medium systems.
    all_students_pool = Student.query.all()
    student_map = {str(s.id): s for s in all_students_pool}
    # Also index by roll_no for any records still using it
    for s in all_students_pool:
        if getattr(s, 'roll_no', None):
            student_map[str(s.roll_no)] = s
    
    stats_map = {} # cid -> {'present', 'absent', 'od', 'leave', 'late'}
    attendee_dept_map = {} # sid -> dept
    
    for sid, statuses in student_today_statuses.items():
        s_obj = student_map.get(sid)
        if not s_obj: continue
        
        cid = str(getattr(s_obj, 'class_id', ''))
        dept = str(getattr(s_obj, 'dept', 'Unknown'))
        attendee_dept_map[sid] = dept
        
        if cid not in stats_map:
            stats_map[cid] = {'present': 0, 'absent': 0, 'od': 0, 'leave': 0, 'late': 0}
            
        if 'Absent' in statuses:
            stats_map[cid]['absent'] += 1
        else:
            stats_map[cid]['present'] += 1
            if 'OD' in statuses: stats_map[cid]['od'] += 1
            elif 'Leave' in statuses: stats_map[cid]['leave'] += 1
            elif 'Late' in statuses: stats_map[cid]['late'] += 1


    if current_user.role == 'in_charge':
        assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
        cls = None
        if assigned_ids:
            all_c = get_cached_metadata('classrooms', Classroom)
            matches = [c for c in all_c if str(getattr(c, 'id', '')) in assigned_ids]
            cls = matches[0] if matches else None

            
        if cls:
            stats = stats_map.get(cls.id, {'present': 0, 'absent': 0})
            in_charge_data = {'present': stats['present'], 'absent': stats['absent'], 'class_name': cls.name}
    
    if current_user.role in ['hod', 'admin', 'teacher']:
        if current_user.role == 'teacher':
            assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
            if not assigned_ids:
                classes_to_show = []
            else:
                all_c = get_cached_metadata('classrooms', Classroom)
                classes_to_show = [c for c in all_c if str(getattr(c, 'id', '')) in assigned_ids]
        else:
            classes_to_show = get_cached_metadata('classrooms', Classroom)


        for cls in classes_to_show:
            stats = stats_map.get(cls.id, {'present': 0, 'absent': 0})
            hod_summary.append({
                'class_name': cls.name, 
                'present': stats['present'], 
                'absent': stats['absent']
            })

    # Department Wise stats
    all_departments = Department.query.all()
    
    # Filter departments for teachers
    if current_user.role in ['teacher', 'in_charge'] and assigned_ids:
        assigned_cls = [c for c in get_cached_metadata('classrooms', Classroom) if str(c.id) in assigned_ids]
        assigned_dept_names = {str(getattr(c, 'dept', '')).lower().strip() for c in assigned_cls}
        departments_to_show = [d for d in all_departments if str(d.name).lower().strip() in assigned_dept_names]
    else:
        departments_to_show = all_departments

    dept_stats = []
    
    # Use cached counts correctly based on visibility
    for d in departments_to_show:
        if current_user.role in ['teacher', 'in_charge'] and assigned_ids:
            # For teacher, "Total" in dept only counts students in THEIR assigned classes within that dept
            total_dept_students = Student.query.filter_by(dept=d.name).where('class_id', 'in', assigned_ids).count()
        else:
            # For HOD/Admin, show overall dept count
            if 'dept_counts' not in _cache or (datetime.now().timestamp() - _cache['dept_counts']['time']) > CACHE_TIMEOUT:
                dept_counts = {}
                for dd in all_departments:
                    dept_counts[dd.name] = Student.query.filter_by(dept=dd.name).count()
                _cache['dept_counts'] = {'data': dept_counts, 'time': datetime.now().timestamp()}
            total_dept_students = _cache['dept_counts']['data'].get(d.name, 0)

        dept_present = 0
        dept_absent = 0
        dept_late = 0
        
        for sid, statuses in student_today_statuses.items():
            s_dept = str(attendee_dept_map.get(sid, '')).lower().strip()
            if s_dept == str(d.name).lower().strip():
                # For teacher, filter by their assigned classes
                if current_user.role in ['teacher', 'in_charge'] and assigned_ids:
                    s_obj = student_map.get(sid)
                    if not s_obj or str(getattr(s_obj, 'class_id', '')) not in assigned_ids:
                        continue
                
                if 'Absent' in statuses:
                    dept_absent += 1
                else:
                    dept_present += 1
                    if 'Late' in statuses: dept_late += 1
        
        dept_stats.append({
            'name': d.name,
            'code': d.code,
            'total_students': total_dept_students,
            'present': dept_present,
            'absent': dept_absent,
            'late': dept_late
        })

    # Calculate Overall Today's Summary
    total_present_today = 0
    for sid, statuses in student_today_statuses.items():
        if 'Absent' not in statuses:
            # Check if student belongs to user scope
            if current_user.role in ['teacher', 'in_charge'] and assigned_ids:
                s_obj = student_map.get(sid)
                if not s_obj or str(getattr(s_obj, 'class_id', '')) not in assigned_ids:
                    continue
            total_present_today += 1
            
    today_attendance_perc = 0
    if total_students > 0:
        today_attendance_perc = min(100.0, round((total_present_today / total_students) * 100, 1))
    
    # Pre-fetch all depts once for UI if needed
    all_depts_list = get_cached_metadata('departments', Department)

    return render_template('dashboard.html', 
                           total_students=total_students, 
                           total_present_today=total_present_today,
                           today_attendance_perc=today_attendance_perc,
                           total_teachers=total_teachers,
                           report_data=report_data,
                           in_charge_data=in_charge_data,
                           hod_summary=hod_summary,
                           dept_stats=dept_stats,
                           today_date=today.strftime('%d %b, %Y'))

@app.route('/class_incharge')
@login_required
def class_incharge():
    if current_user.role not in ['admin', 'hod', 'in_charge']:
        flash('Unauthorized Access!', 'danger')
        return redirect(url_for('dashboard'))
        
    assigned_ids = [str(x) for x in getattr(current_user, 'assigned_classes', []) if x]
    
    # If admin/hod and no class selected, show first class or selection
    selected_class_id = request.args.get('class_id')
    
    if current_user.role in ['admin', 'hod']:
        all_classes = Classroom.query.all()
        if not selected_class_id and all_classes:
            selected_class_id = str(all_classes[0].id)
    else:
        if not assigned_ids:
            flash('No class assigned to you as In-charge.', 'warning')
            return redirect(url_for('dashboard'))
        if not selected_class_id:
            selected_class_id = assigned_ids[0]
        elif selected_class_id not in assigned_ids:
            flash('Unauthorized Access to this class.', 'danger')
            return redirect(url_for('dashboard'))

    if not selected_class_id:
        flash('No classes available.', 'info')
        return redirect(url_for('dashboard'))

    cls = Classroom.query.get_or_404(selected_class_id)
    students = Student.query.filter_by(class_id=selected_class_id).all()
    
    # Sort students by roll no
    students.sort(key=lambda x: str(x.roll_no).lower())
    
    # Fetch subjects for this class (by Dept and Semester)
    class_dept = getattr(cls, 'dept', '')
    class_semester = str(getattr(cls, 'current_semester', getattr(cls, 'semester', '')))
    
    class_subjects = []
    if class_dept and class_semester:
        class_subjects = Subject.query.filter_by(dept=class_dept, semester=class_semester).all()
        class_subjects.sort(key=lambda x: str(getattr(x, 'name', '')).lower())

    # Today's stats (Day-wise student based)
    today = datetime.utcnow().date()
    today_str = today.strftime('%Y-%m-%d')
    attendance_today = Attendance.query.filter_by(date=today_str, class_id=selected_class_id).all()
    
    # student_id -> set of statuses today
    student_today_statuses = {}
    for rec in attendance_today:
        sid = str(getattr(rec, 'student_id', ''))
        if sid not in student_today_statuses:
            student_today_statuses[sid] = set()
        student_today_statuses[sid].add(getattr(rec, 'status', ''))

    stats = {'present': 0, 'absent': 0, 'od': 0, 'leave': 0, 'late': 0}
    for sid, statuses in student_today_statuses.items():
        if 'Absent' in statuses:
            stats['absent'] += 1
        else:
            stats['present'] += 1
            if 'OD' in statuses: stats['od'] += 1
            elif 'Leave' in statuses: stats['leave'] += 1
            elif 'Late' in statuses: stats['late'] += 1
        
    # Overall student stats + Subject-wise stats
    rep_data = []
    
    # Pre-map today's attendance for quick lookup
    today_status_map = {} # {student_id: {subject_id: status}}
    for rec in attendance_today:
        sid = str(getattr(rec, 'student_id', ''))
        subid = str(getattr(rec, 'subject_id', ''))
        status = getattr(rec, 'status', '').strip()
        if sid not in today_status_map:
            today_status_map[sid] = {}
        today_status_map[sid][subid] = status

    for s in students:
        overall_res = s.get_attendance_stats()
        
        # Determine subject-wise status for today
        subject_today_data = {}
        s_today_recs = today_status_map.get(str(s.id), {})
        
        for sub in class_subjects:
            subject_today_data[sub.id] = s_today_recs.get(sub.id, '-')

        rep_data.append({
            'student': s,
            'total': overall_res[0],
            'present': overall_res[1],
            'percentage': overall_res[2],
            'od': overall_res[3],
            'leave': overall_res[4],
            'late': overall_res[5],
            
            'subject_today': subject_today_data
        })

    return render_template('class_incharge.html', 
                           cls=cls, 
                           students=rep_data, 
                           stats=stats,
                           subjects=class_subjects,
                           all_classes=Classroom.query.all() if current_user.role in ['admin', 'hod'] else None)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        # normalize input to avoid case/spacing issues
        username = request.form.get('username', '').strip().lower()
        password = request.form.get('password')
        
        # 1. Check for missing fields
        if not username or not password:
            flash('Please provide both username and password', 'warning')
            return render_template('login.html')

        try:
            # 2. Fetch user safely (try both username and email fields)
            user = User.query.filter_by(username=username).first()
            if not user:
                # allow login using email if someone types it
                user = User.query.filter_by(email=username).first()
            
            # debug output to console (helpful during development)
            if not user:
                print(f"Login attempt: no user found for '{username}'")
            else:
                print(f"Login attempt: found user id={user.id} username={user.username} role={user.role}")
            
            # 3. Check existence and password safely
            if user and check_password_hash(getattr(user, 'password', ''), password):
                login_user(user)
                
                # 4. Explicit session handling as requested
                session['user_id'] = user.id
                session.permanent = True
                
                flash(f"Welcome back, {user.username}!", 'success')
                
                if user.role in ['admin', 'teacher', 'hod', 'in_charge']:
                    return redirect(url_for('dashboard'))
                elif user.role == 'security':
                    return redirect(url_for('security_portal'))
                return redirect(url_for('student_dashboard'))
            else:
                # provide more specific feedback
                if user is None:
                    flash('User not found. Please contact administrator.', 'danger')
                else:
                    flash('Incorrect password. Please try again.', 'danger')
        except Exception as e:
            print(f"Login error: {e}")
            flash('An internal error occurred. Please try again later.', 'danger')
            return render_template('login.html')
            
    return render_template('login.html')

@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))

# -- Teacher Management (Admin Only) --
@app.route('/teachers', methods=['GET', 'POST'])
@login_required
@admin_required
def teachers():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password') or 'teacher123'
        role = request.form.get('role', 'teacher')
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already exists!', 'danger')
        else:
            phone = request.form.get('phone')
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            # store credentials normalized to lowercase to avoid case mismatch
            normalized_email = email.strip().lower()
            new_teacher = User(name=name, email=normalized_email, username=normalized_email, password=hashed_pw, role=role, phone=phone)
            new_teacher.save()
            flash(f'Teacher {name} added successfully!', 'success')
        return redirect(url_for('teachers'))
    
    all_teachers = User.query.where('role', 'in', ['teacher', 'in_charge']).all()
    # Sort teachers by name
    all_teachers.sort(key=lambda x: x.name.lower())
    all_classes = Classroom.query.all()
    class_map = {str(c.id): c.name for c in all_classes}
    return render_template('teachers.html', teachers=all_teachers, class_map=class_map)

@app.route('/teachers/bulk_upload', methods=['POST'])
@login_required
@admin_required
def bulk_upload_teachers():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('teachers'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('teachers'))
    
    if file and (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
        try:
            if file.filename.endswith('.csv'):
                try:
                    content = file.stream.read().decode("UTF-8")
                except UnicodeDecodeError:
                    file.stream.seek(0)
                    content = file.stream.read().decode("latin1")
                stream = io.StringIO(content, newline=None)
                df = pd.read_csv(stream)
            else:
                df = pd.read_excel(file)
            
            df.columns = [str(c).strip() for c in df.columns]
            required_cols = ['Name', 'Email']
            for col in required_cols:
                if col not in df.columns:
                    flash(f'Missing required column: {col}', 'danger')
                    return redirect(url_for('teachers'))
            
            existing_users = {str(getattr(u, 'email', '')).strip().lower() for u in User.query.all() if getattr(u, 'email', None)}
            all_classes_map = {str(c.name).strip().lower(): str(c.id) for c in Classroom.query.all()}
            success_count = 0
            
            for _, row in df.iterrows():
                name = str(row['Name']).strip()
                email = str(row['Email']).strip().lower()
                phone = str(row.get('Phone', '')).strip() if 'Phone' in df.columns else None
                password = str(row.get('Password', 'teacher123')).strip()
                classes_str = str(row.get('Classes', '')).strip() if 'Classes' in df.columns else ""
                
                # Resolve class names to IDs
                assigned_classes = []
                if classes_str and classes_str.lower() != 'nan':
                    # Support comma or semicolon separated names
                    class_names = [n.strip().lower() for n in classes_str.replace(';', ',').split(',') if n.strip()]
                    for name_item in class_names:
                        cid = all_classes_map.get(name_item)
                        if cid:
                            assigned_classes.append(cid)
                
                if not name or not email or email in existing_users:
                    continue
                
                hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
                new_teacher = User(
                    name=name, 
                    email=email, 
                    username=email.strip().lower(), 

                    password=hashed_pw, 
                    role='teacher', 
                    phone=phone,
                    assigned_classes=assigned_classes
                )
                new_teacher.save()
                existing_users.add(email)
                success_count += 1
                
            flash(f'Successfully imported {success_count} teachers!', 'success')
        except Exception as e:
            flash(f'Error processing file: {e}', 'danger')
            
    return redirect(url_for('teachers'))

@app.route('/classes', methods=['GET', 'POST'])
@login_required
@admin_required
def classes():
    if request.method == 'POST':
        name = request.form.get('name')
        dept = request.form.get('dept')
        year = request.form.get('year')
        current_sem = request.form.get('current_semester', 1)
        new_class = Classroom(name=name, dept=dept, year=year, current_semester=current_sem)
        new_class.save()
        flash(f'Class {name} added!', 'success')
        return redirect(url_for('classes'))
    all_classes = Classroom.query.all()
    # Sort classes by name
    all_classes.sort(key=lambda x: x.name.lower())
    departments = Department.query.all()
    return render_template('classes.html', classes=all_classes, departments=departments)

@app.route('/delete_class/<id>')

@login_required
@admin_required
def delete_class(id):
    cls = Classroom.query.get_or_404(id)
    if cls:
        cls.delete()
        flash('Class deleted!', 'info')
    return redirect(url_for('classes'))

@app.route('/edit_class/<id>', methods=['POST'])

@login_required
@admin_required
def edit_class(id):
    try:
        cls = Classroom.query.get_or_404(id)
        cls.update(
            name=request.form.get('name'),
            dept=request.form.get('dept'),
            current_semester=request.form.get('current_semester'),
            year=request.form.get('year')
        )
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'success', 'message': 'Class updated!'})
        flash('Class updated!', 'success')
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f'Error updating class: {str(e)}', 'danger')
    return redirect(url_for('classes'))

@app.route('/api/get_classes/<dept>')
@login_required
def api_get_classes(dept):
    classes = Classroom.query.filter_by(dept=dept).all()
    return jsonify([{'id': c.id, 'name': c.name} for c in classes])

@app.route('/api/get_semesters/<class_id>')

@login_required
def api_get_semesters(class_id):
    # This might be fixed or dynamic; for now return 1-8 but could be limited by class year
    return jsonify([{'id': i, 'name': f'Semester {i}'} for i in range(1, 9)])
@app.route('/api/get_class_details/<class_id>')

@login_required
def get_class_details(class_id):
    cls = Classroom.query.get_or_404(class_id)
    return jsonify({
        'id': cls.id,
        'name': cls.name,
        'dept': cls.dept,
        'current_semester': cls.current_semester,
        'year': cls.year
    })

@app.route('/api/get_students_by_class/<class_id>')

@login_required
def get_students_by_class(class_id):
    students = Student.query.filter_by(class_id=class_id).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'roll_no': s.roll_no
    } for s in students])

@app.route('/api/get_all_classes')
@login_required
def get_all_classes():
    classes = Classroom.query.all()
    return jsonify([{'id': c.id, 'name': c.name, 'dept': c.dept} for c in classes])

@app.route('/api/get_subjects_by_semester/<semester_id>/<dept>')
@login_required
def get_subjects_by_semester(semester_id, dept):
    # Fetch all subjects for this semester (trying both string and int)
    subjects_for_sem = Subject.query.filter_by(semester=semester_id).all()
    if not subjects_for_sem:
        try:
            subjects_for_sem = Subject.query.filter_by(semester=int(semester_id)).all()
        except (ValueError, TypeError):
            subjects_for_sem = []
    
    # Filter by department in Python (case-insensitive and stripped)
    filtered = [
        s for s in subjects_for_sem 
        if str(getattr(s, 'dept', '')).lower().strip() == dept.lower().strip()
    ]
            
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'code': s.code
    } for s in filtered])

@app.route('/delete_teacher/<id>')

@login_required
@admin_required
def delete_teacher(id):
    teacher = User.query.get_or_404(id)
    if teacher and teacher.role in ['teacher', 'in_charge']:
        teacher.delete()
        flash('Teacher removed!', 'info')
    else:
        flash('Cannot remove this user!', 'danger')
    return redirect(url_for('teachers'))

@app.route('/edit_teacher/<id>', methods=['POST'])

@login_required
def edit_teacher(id):
    if current_user.role != 'admin' and current_user.id != id:
        return jsonify({'status': 'error', 'message': 'Unauthorized'}), 403
        
    teacher = User.query.get_or_404(id)
    update_data = {
        'name': request.form.get('name'),
        'email': request.form.get('email'),
        'username': request.form.get('email'),
        'phone': request.form.get('phone'),
        'role': request.form.get('role', teacher.role)
    }
    if request.form.get('password'):
        update_data['password'] = generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
    
    teacher.update(**update_data)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': 'Teacher updated successfully!'})
        
    flash('Teacher updated successfully!', 'success')
    return redirect(url_for('teachers'))


@app.route('/api/assign_classes/<teacher_id>', methods=['POST'])

@login_required
@admin_required
def assign_classes(teacher_id):
    teacher = User.query.get_or_404(teacher_id)
    class_ids = request.form.getlist('class_ids')
    
    # Update assignments with IDs
    teacher.assigned_classes = class_ids
    
    teacher.save()
    return jsonify({'status': 'success', 'message': 'Classes assigned successfully!'})

@app.route('/api/teachers/<id>', methods=['GET'])

@login_required
@admin_required
def get_teacher_data(id):
    teacher = User.query.get_or_404(id)
    return jsonify({
        'id': teacher.id,
        'name': teacher.name,
        'email': getattr(teacher, 'email', ''),
        'phone': teacher.phone,
        'assigned_classes': teacher.assigned_classes
    })

@app.route('/profile')
@login_required
def profile():
    if current_user.role == 'student':
        flash('Students are not allowed to edit their profile.', 'danger')
        return redirect(url_for('student_dashboard'))
    return render_template('profile.html')

@app.route('/api/profile', methods=['GET', 'POST'])
@login_required
def api_profile():
    if request.method == 'POST':
        user = current_user
        user.name = request.form.get('name')
        # normalize email/username when changed
        new_email = request.form.get('email', '').strip().lower()
        user.email = new_email
        user.username = new_email
        user.phone = request.form.get('phone')
        if request.form.get('password'):
            user.password = generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
        user.save()
        return jsonify({'status': 'success', 'message': 'Profile updated!'})
    
    return jsonify({
        'name': current_user.name,
        'email': getattr(current_user, 'email', ''),
        'phone': current_user.phone,
        'username': current_user.username,
        'role': current_user.role
    })

# -- Student Management --
@app.route('/students', methods=['GET', 'POST'])
@login_required
@admin_required
def students():
    if request.method == 'POST':
        name = request.form.get('name')
        roll_no = request.form.get('roll_no')
        dept = request.form.get('dept')
        phone = request.form.get('phone')
        class_id = request.form.get('class_id')
        semester = 1
        if class_id:
            cls = Classroom.query.get(class_id)
            if cls:
                semester = cls.current_semester

        # Create Student Record
        new_student = Student(name=name, roll_no=roll_no, dept=dept, phone=phone, semester=semester, class_id=class_id)
        new_student.save()
        
        # Create User Login for Student
        default_pw = generate_password_hash('student123', method='pbkdf2:sha256')
        new_user = User(name=name, username=roll_no, password=default_pw, role='student', student_id=new_student.id, phone=phone)
        new_user.save()

        flash(f'Student {name} added! Login: {roll_no} / student123', 'success')
        return redirect(url_for('students'))
    
    all_students = Student.query.all()
    # Sort students by roll no
    all_students.sort(key=lambda x: x.roll_no.lower())
    all_classes = Classroom.query.all()
    all_depts = Department.query.all()
    class_map = {str(c.id): c.name for c in all_classes}
    return render_template('students.html', students=all_students, classes=all_classes, class_map=class_map, departments=all_depts)

@app.route('/delete_student/<id>')

@login_required
@admin_required
def delete_student(id):
    student = Student.query.get_or_404(id)
    if student:
        # Also delete the associated user account
        user = User.query.filter_by(student_id=student.id).first()
        if user:
            user.delete()
        student.delete()
        flash('Student deleted!', 'info')
    return redirect(url_for('students'))

@app.route('/edit_student/<id>', methods=['POST'])

@login_required
@admin_required
def edit_student(id):
    student = Student.query.get_or_404(id)
    class_id = request.form.get('class_id')
    semester = request.form.get('semester', student.semester)
    if class_id:
        cls = Classroom.query.get(class_id)
        if cls:
            semester = cls.current_semester

    update_data = {
        'name': request.form.get('name'),
        'roll_no': request.form.get('roll_no'),
        'dept': request.form.get('dept'),
        'phone': request.form.get('phone'),
        'semester': semester,
        'class_id': class_id
    }
    student.update(**update_data)
    
    # Update corresponding User account
    user = User.query.filter_by(student_id=student.id).first()
    if user:
        user_update = {'username': student.roll_no}
        if request.form.get('password'):
            user_update['password'] = generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
        user.update(**user_update)
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': 'Student updated successfully!'})
    
    flash('Student updated successfully!', 'success')
    return redirect(url_for('students'))

@app.route('/api/students/<id>', methods=['GET'])

@login_required
@admin_required
def get_student(id):
    student = Student.query.get_or_404(id)
    return jsonify({
        'id': student.id,
        'name': student.name,
        'roll_no': student.roll_no,
        'dept': student.dept,
        'semester': student.semester,
        'phone': student.phone,
        'class_id': student.class_id
    })

@app.route('/admin/students/import', methods=['POST'])
@app.route('/students/bulk_upload', methods=['POST'])
@login_required
@admin_required
def bulk_upload_students():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('students'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('students'))
    
    if file and (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
        try:
            if file.filename.endswith('.csv'):
                # Try UTF-8 first, fallback to latin1 for Excel-generated CSVs
                try:
                    content = file.stream.read().decode("UTF-8")
                except UnicodeDecodeError:
                    file.stream.seek(0)
                    content = file.stream.read().decode("latin1")
                stream = io.StringIO(content, newline=None)
                df = pd.read_csv(stream)
            else:
                df = pd.read_excel(file)
            
            # Clean column names (strip whitespace)
            df.columns = [str(c).strip() for c in df.columns]
            
            # Required columns: Name, Roll No, Dept, Class_Name
            required_cols = ['Name', 'Roll No', 'Dept', 'Class_Name']
            for col in required_cols:
                if col not in df.columns:
                    flash(f'Missing required column: {col}', 'danger')
                    return redirect(url_for('students'))
            
            # Pre-fetch for performance (O(1) lookups instead of O(N) queries)
            all_classes = {str(c.name).strip(): c for c in Classroom.query.all()}
            existing_roll_nos = {str(s.roll_no).strip() for s in Student.query.all()}
            existing_users = {str(u.username).strip(): u for u in User.query.all()}
            
            db_conn = get_db()
            batch = db_conn.batch()
            batch_ops = 0
            
            success_count = 0
            skipped_count = 0
            
            for _, row in df.iterrows():
                name = str(row['Name']).strip()
                roll_no = str(row['Roll No']).strip()
                dept = str(row['Dept']).strip()
                email = str(row.get('Email', '')).strip()
                phone = str(row.get('Phone', '')) if 'Phone' in df.columns else None
                class_name = str(row.get('Class_Name', '')).strip()
                
                if not roll_no or roll_no.lower() == 'nan' or not name or name.lower() == 'nan':
                    print(f"DEBUG: Skipping {name}/{roll_no} - missing name or roll_no")
                    skipped_count += 1
                    continue
                
                if roll_no in existing_roll_nos:
                    print(f"DEBUG: Skipping {name}/{roll_no} - already exists in database")
                    skipped_count += 1
                    continue

                cls = all_classes.get(class_name)
                if not cls:
                    # Try case-insensitive match
                    cls = next((c for n, c in all_classes.items() if n.lower() == class_name.lower()), None)
                
                if not cls:
                    print(f"DEBUG: Creating missing class {class_name} for student {name}")
                    # Auto-create classroom if missing (Rectify)
                    try:
                        new_cls_ref = db_conn.collection(Classroom.__collection__).document()
                        
                        # Infer year/semester (I=1/1, II=2/3, III=3/5, IV=4/7)
                        year, sem = "1", "1"
                        if "IV" in class_name: year, sem = "4", "7"
                        elif "III" in class_name: year, sem = "3", "5"
                        elif "II" in class_name: year, sem = "2", "3"
                        
                        cls = Classroom(
                            id=new_cls_ref.id,
                            name=class_name,
                            dept=dept,
                            year=year,
                            current_semester=sem
                        )
                        batch.set(new_cls_ref, cls.to_dict())
                        batch_ops += 1
                        all_classes[class_name] = cls # Add to cache for next rows
                    except Exception as e:
                        print(f"Error creating class {class_name}: {e}")
                        skipped_count += 1
                        continue

                try:
                    # 1. Pre-generate Student ID for linking
                    student_ref = db_conn.collection(Student.__collection__).document()
                    new_student = Student(
                        id=student_ref.id, 
                        name=name, 
                        roll_no=roll_no, 
                        dept=dept, 
                        phone=phone, 
                        semester=cls.current_semester, 
                        class_id=cls.id
                    )
                    
                    # Add student to batch
                    batch.set(student_ref, new_student.to_dict())
                    batch_ops += 1

                    # 2. Check User association
                    user = existing_users.get(roll_no)
                    if user:
                        user_ref = db_conn.collection(User.__collection__).document(str(user.id))
                        # Update existing user
                        update_data = {'student_id': new_student.id, 'name': name}
                        if phone: update_data['phone'] = phone
                        batch.update(user_ref, update_data)
                    else:
                        # Create new user login
                        user_ref = db_conn.collection(User.__collection__).document()
                        default_pw = generate_password_hash('student123', method='pbkdf2:sha256')
                        new_user = User(
                            name=name,
                            username=roll_no, 
                            email=email if email else None,
                            password=default_pw, 
                            role='student', 
                            student_id=new_student.id, 
                            phone=phone
                        )
                        batch.set(user_ref, new_user.to_dict())
                    
                    batch_ops += 1
                    success_count += 1
                    existing_roll_nos.add(roll_no) # Prevent internal duplicates in same file

                    # Firestore batch limit is 500
                    if batch_ops >= 450:
                        batch.commit()
                        batch = db_conn.batch()
                        batch_ops = 0

                except Exception as e:
                    print(f"Error processing {name}/{roll_no}: {e}")
                    skipped_count += 1
            
            if batch_ops > 0:
                batch.commit()
            
            if success_count > 0:
                flash(f'Successfully imported {success_count} students!', 'success')
            if skipped_count > 0:
                flash(f'Skipped {skipped_count} students. Check logs for details.', 'warning')
        except Exception as e:
            import traceback
            traceback.print_exc()
            flash(f'Error processing file: {str(e)}', 'danger')
    else:
        flash('Invalid file format. Please upload CSV or XLSX.', 'danger')
        
    return redirect(url_for('students'))

@app.route('/students/download_template')
@login_required
@admin_required
def download_student_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Roll No', 'Dept', 'Email', 'Class_Name', 'Phone'])
    writer.writerow(['John Doe', 'CS2023001', 'Computer Science', 'john@example.com', 'I-CSE', '9876543210'])
    writer.writerow(['Jane Smith', 'EC2023001', 'Electronics', 'jane@example.com', 'II-ECE', '9876543211'])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name='student_template.csv'
    )

@app.route('/admin/teachers/template')
@login_required
@admin_required
def download_teacher_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Email', 'Phone', 'Password', 'Classes'])
    writer.writerow(['Dr. Ramesh', 'ramesh@college.edu', '9876543210', 'staff123', 'I-CSC, II-CSC'])
    writer.writerow(['Ms. Priya', 'priya@college.edu', '9876543211', 'priya2024', 'I-BCOM'])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name='teacher_template.csv'
    )

# -- Subject Management --
@app.route('/subjects', methods=['GET', 'POST'])
@login_required
@admin_required
def subjects():
    if request.method == 'POST':
        name = request.form.get('name')
        code = request.form.get('code')
        dept = request.form.get('dept')
        semester = request.form.get('semester')
        teacher_id = request.form.get('teacher_id')
        new_subject = Subject(name=name, code=code, dept=dept, semester=semester, 
                            teacher_id=teacher_id if teacher_id else None)
        new_subject.save()
        flash('Subject added successfully!', 'success')
        return redirect(url_for('subjects'))
    
    selected_semester = request.args.get('semester')
    selected_dept = request.args.get('dept')
    
    query = Subject.query
    if selected_semester:
        query = query.filter_by(semester=selected_semester)
    if selected_dept:
        query = query.filter_by(dept=selected_dept)
    
    all_subjects = query.all()
    
    # Sort subjects by Semester (asc) then Name
    def sort_key(s):
        try:
            sem = int(getattr(s, 'semester', 0))
        except:
            sem = 0
        return (sem, str(getattr(s, 'name', '')).lower())
        
    all_subjects.sort(key=sort_key)
    
    all_teachers = User.query.filter_by(role='teacher').all()
    departments = get_cached_metadata('departments', Department)

    return render_template('subjects.html', subjects=all_subjects, teachers=all_teachers, 
                          selected_semester=selected_semester, selected_dept=selected_dept,
                          departments=departments)

@app.route('/subjects/bulk_upload', methods=['POST'])
@login_required
@admin_required
def bulk_upload_subjects():
    if 'file' not in request.files:
        flash('No file part', 'danger')
        return redirect(url_for('subjects'))
    
    file = request.files['file']
    if file.filename == '':
        flash('No selected file', 'danger')
        return redirect(url_for('subjects'))
    
    if file and (file.filename.endswith('.csv') or file.filename.endswith('.xlsx')):
        try:
            if file.filename.endswith('.csv'):
                try:
                    content = file.stream.read().decode("UTF-8")
                except UnicodeDecodeError:
                    file.stream.seek(0)
                    content = file.stream.read().decode("latin1")
                stream = io.StringIO(content, newline=None)
                df = pd.read_csv(stream)
            else:
                df = pd.read_excel(file)
            
            df.columns = [str(c).strip() for c in df.columns]
            required_cols = ['Name', 'Code', 'Dept', 'Semester']
            for col in required_cols:
                if col not in df.columns:
                    flash(f'Missing required column: {col}', 'danger')
                    return redirect(url_for('subjects'))
            
            teachers_map = {str(getattr(u, 'email', '')).strip().lower(): u.id for u in User.query.filter_by(role='teacher').all() if getattr(u, 'email', None)}
            success_count = 0
            
            for _, row in df.iterrows():
                name = str(row['Name']).strip()
                code = str(row['Code']).strip()
                dept = str(row['Dept']).strip()
                semester = str(row['Semester']).strip()
                teacher_email = str(row.get('Teacher_Email', '')).strip().lower()
                
                if not name or not code:
                    continue
                
                teacher_id = teachers_map.get(teacher_email)
                new_subject = Subject(
                    name=name, 
                    code=code, 
                    dept=dept, 
                    semester=semester, 
                    teacher_id=teacher_id
                )
                new_subject.save()
                success_count += 1
                
            flash(f'Successfully imported {success_count} subjects!', 'success')
        except Exception as e:
            flash(f'Error processing file: {e}', 'danger')
            
    return redirect(url_for('subjects'))

@app.route('/admin/subjects/template')
@login_required
@admin_required
def download_subject_template():
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(['Name', 'Code', 'Dept', 'Semester', 'Teacher_Email'])
    writer.writerow(['Data Structures', 'CS101', 'Computer Science', '3', 'ramesh@college.edu'])
    writer.writerow(['Business Math', 'BM202', 'B.COM', '2', 'priya@college.edu'])
    
    output.seek(0)
    return send_file(
        io.BytesIO(output.getvalue().encode()),
        mimetype='text/csv',
        as_attachment=True,
        download_name='subject_template.csv'
    )


@app.route('/delete_subject/<id>')

@login_required
@admin_required
def delete_subject(id):
    try:
        subject = Subject.query.get_or_404(id)
        if subject:
            subject.delete()
            flash('Subject deleted!', 'info')
    except Exception as e:
        flash(f'Error deleting subject: {str(e)}', 'danger')
    return redirect(url_for('subjects'))

@app.route('/edit_subject/<id>', methods=['POST'])

@login_required
@admin_required
def edit_subject(id):
    subject = Subject.query.get_or_404(id)
    subject.name = request.form.get('name')
    subject.code = request.form.get('code')
    subject.dept = request.form.get('dept')
    subject.semester = request.form.get('semester')
    teacher_id = request.form.get('teacher_id')
    subject.teacher_id = teacher_id if teacher_id else None
    
    subject.save()
    
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({'status': 'success', 'message': 'Subject updated successfully!'})
    
    flash('Subject updated successfully!', 'success')
    return redirect(url_for('subjects'))

@app.route('/api/subjects/get/<id>', methods=['GET'])

@login_required
@admin_required
def get_subject_data_api(id):
    subject = Subject.query.get_or_404(id)
    return jsonify({
        'id': subject.id,
        'name': subject.name,
        'code': subject.code,
        'dept': subject.dept,
        'semester': subject.semester,
        'teacher_id': subject.teacher_id
    })

# -- Attendance Marking --
@app.route('/attendance', methods=['GET', 'POST'])
@login_required
@teacher_allowed
def attendance():
    # Fetch classes available to this user
    if current_user.role == 'admin':
        all_classes = Classroom.query.all()
    else:
        assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
        if not assigned_ids:
            all_classes = []
        else:
            all_c = get_cached_metadata('classrooms', Classroom)
            all_classes = [c for c in all_c if str(getattr(c, 'id', '')) in assigned_ids]


    if request.method == 'POST':
        subject_id = request.form.get('subject_id')
        class_id = request.form.get('class_id')
        subject = Subject.query.get(subject_id)
        if not subject or not class_id:
            flash('Invalid Subject or Class', 'danger')
            return redirect(url_for('attendance'))
            
        date_str = request.form.get('date')
        if not date_str:
            flash('Date is required', 'danger')
            return redirect(url_for('attendance'))
            
        # Fetch students for the SELECTED CLASS only
        students = Student.query.filter_by(class_id=class_id).all()
        student_ids = {str(s.id) for s in students}
        
        # Use Firestore Batch Write for performance
        db_conn = get_db()
        batch = db_conn.batch()
        count = 0
        
        # Valid status options
        VALID_STATUSES = ['Present', 'Absent', 'OD', 'Leave', 'Late']
        
        for student in students:
            status = request.form.get(f'status_{student.id}')
            # Validate status and ensure student belongs to this class
            if status in VALID_STATUSES: 
                new_record = Attendance(
                    student_id=str(student.id),
                    subject_id=str(subject_id),
                    class_id=str(class_id),
                    teacher_id=str(current_user.id),
                    date=date_str, # Store as string YYYY-MM-DD
                    status=status
                )
                # Prepare batch write
                doc_ref = db_conn.collection(Attendance.__collection__).document()
                batch.set(doc_ref, new_record.to_dict())
                count += 1

        if count > 0:
            batch.commit()

        flash(f'Attendance marked for {count} students!', 'success')
        return redirect(url_for('dashboard'))

    return render_template('attendance.html', 
                         classes=all_classes, 
                         today=datetime.utcnow().strftime('%Y-%m-%d'))

@app.route('/get_students_by_subject/<subject_id>')

@login_required
@teacher_allowed
def get_students_by_subject(subject_id):
    subject = Subject.query.get_or_404(subject_id)
    if not subject.dept:
        return jsonify([])
    students = Student.query.filter_by(dept=subject.dept).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'roll_no': s.roll_no
    } for s in students])

@app.route('/api/subjects/<semester_id>')

@login_required
def api_subjects_by_semester(semester_id):
    subjects = Subject.query.filter_by(semester=semester_id).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'code': s.code,
        'dept': s.dept
    } for s in subjects])

@app.route('/admin/attendance-shortcut')
@login_required
def attendance_shortcut():
    if current_user.role in ['admin', 'teacher']:
        return redirect(url_for('attendance'))
    return redirect(url_for('student_dashboard'))

@app.route('/attendance/history')
@login_required
def attendance_history():
    selected_date = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    class_id = request.args.get('class_id')
    
    # Metadata for filters
    all_classes_objs = get_cached_metadata('classrooms', Classroom)
    departments = get_cached_metadata('departments', Department)
    all_subjects = {str(s.id): s for s in get_cached_metadata('subjects', Subject)}
    
    # Permission guards
    is_staff = current_user.role in ['admin', 'hod', 'teacher', 'in_charge']
    
    if current_user.role == 'student':
        student = Student.query.get(current_user.student_id)
        if not student:
            flash('Student record not found.', 'danger')
            return redirect(url_for('dashboard'))
            
        # Robust Fetch: Try doc ID first, then roll_no as fallback for older records
        records_by_id = Attendance.query.filter_by(student_id=str(student.id)).all()
        records_by_roll = Attendance.query.filter_by(student_id=str(student.roll_no)).all()
        # Merge and deduplicate by document ID
        all_recs_map = {r.id: r for r in (records_by_id + records_by_roll)}
        all_recs = list(all_recs_map.values())
        
        # Filter for the selected date
        records = [r for r in all_recs if str(getattr(r, 'date', '')).strip() == str(selected_date).strip()]
        
        # Get list of all unique dates the student has records for (for navigation help)
        available_dates = sorted(list({str(getattr(r, 'date', '')) for r in all_recs if getattr(r, 'date', None)}), reverse=True)
        
        history = []
        for r in records:
            sub = all_subjects.get(str(r.subject_id))
            history.append({
                'subject_name': sub.name if sub else ('General/Gate' if str(r.subject_id) == 'GLOBAL' else 'Other Session'),
                'subject_code': sub.code if sub else (str(r.subject_id) if str(r.subject_id) == 'GLOBAL' else 'N/A'),
                'status': r.status,
                'marked_by': User.query.get(r.teacher_id).name if r.teacher_id and User.query.get(r.teacher_id) else 'System'
            })
            
        return render_template('daily_attendance.html', 
                             date=selected_date, 
                             history=history,
                             student=student,
                             available_dates=available_dates)
                             
    elif is_staff:
        # Staff can see class-wise attendance for a day
        if current_user.role in ['teacher', 'in_charge']:
            assigned_ids = [str(x) for x in getattr(current_user, 'assigned_classes', []) if x is not None]
            classes = [c for c in all_classes_objs if str(c.id) in assigned_ids]
        else:
            classes = all_classes_objs
            
        # If class_id is not provided but they have exactly 1 assigned class, default to it
        if not class_id and len(classes) == 1:
            class_id = str(classes[0].id)

        students_data = []
        subjects_today = []
        
        if class_id:
            # Get students in this class
            students = Student.query.filter_by(class_id=class_id).all()
            students.sort(key=lambda x: str(getattr(x, 'roll_no', '')).lower())
            
            # Get all attendance records for this class on this date
            records = Attendance.query.filter_by(class_id=class_id, date=selected_date).all()
            
            # Map student_id -> subject_id -> record
            attendance_map = {}
            found_subject_ids = set()
            for r in records:
                sid = str(r.student_id)
                subid = str(r.subject_id)
                if sid not in attendance_map:
                    attendance_map[sid] = {}
                attendance_map[sid][subid] = r
                found_subject_ids.add(subid)
            
            # List of subjects that have records today
            subjects_today = [all_subjects[sid] for sid in found_subject_ids if sid in all_subjects]
            if 'GLOBAL' in found_subject_ids:
                # Add a dummy subject for global entries
                subjects_today.append(Subject(id='GLOBAL', name='Global/Late', code='GLB'))
                
            for s in students:
                s_attendance = []
                for sub in subjects_today:
                    rec = attendance_map.get(str(s.id), {}).get(str(sub.id))
                    s_attendance.append(rec.status if rec else '-')
                
                students_data.append({
                    'student': s,
                    'attendance': s_attendance
                })
        
        return render_template('daily_attendance.html',
                             date=selected_date,
                             classes=classes,
                             class_id=class_id,
                             subjects_today=subjects_today,
                             students_data=students_data,
                             departments=departments)
    
    else:
        abort(403)

# -- Reports --
@app.route('/reports')
@login_required
@teacher_allowed
def reports():
    # Filtering parameters
    dept = request.args.get('dept')
    class_id = request.args.get('class_id')
    semester = request.args.get('semester', type=int)
    subject_id = request.args.get('subject_id')
    teacher_id = request.args.get('teacher_id')

    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # Base Student Query - Use server-side filtering where possible
    s_query = Student.query
    if class_id:
        s_query = s_query.filter_by(class_id=class_id)
    if dept:
        s_query = s_query.filter_by(dept=dept)
    
    if current_user.role == 'teacher':
        assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
        if assigned_ids:
            if len(assigned_ids) == 1:
                s_query = s_query.filter_by(class_id=assigned_ids[0])
            elif len(assigned_ids) <= 10:
                s_query = s_query.where('class_id', 'in', assigned_ids)
            # Else we filter in python for > 10 classes
    
    all_students_pool = s_query.all()
    students = []
    
    for s in all_students_pool:
        # Semester Filter (Python side as it's less common to index)
        if semester:
            s_sem = str(getattr(s, 'semester', ''))
            if s_sem != str(semester):
                continue
        
        # Secondary Python filter for large assigned_ids sets
        if current_user.role == 'teacher':
             assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
             if len(assigned_ids) > 10:
                 if str(getattr(s, 'class_id', '')) not in assigned_ids:
                     continue
                 
        students.append(s)
    
    # Base Attendance Query for Logs
    attendance_query = Attendance.query
    if subject_id: attendance_query = attendance_query.filter_by(subject_id=subject_id)
    if teacher_id: attendance_query = attendance_query.filter_by(teacher_id=teacher_id)
    
    # Use datetime objects (midnight) for consistent filtering with Firestore Timestamps
    if start_date: 
        s_dt = datetime.strptime(start_date, '%Y-%m-%d')
        attendance_query = attendance_query.where('date', '>=', s_dt)
    if end_date:
        e_dt = datetime.strptime(end_date, '%Y-%m-%d')
        # To include the entire end date, comparison is usually <= midnight of that day 
        # given that Attendance records are also saved as midnight.
        attendance_query = attendance_query.where('date', '<=', e_dt)
    
    if current_user.role == 'teacher':
        # Limit to assigned classes' students with an empty list guard
        assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
        if not assigned_ids:
            attendance_query = attendance_query.where('class_id', '==', '__none__')
        elif len(assigned_ids) <= 10:
            if len(assigned_ids) == 1:
                attendance_query = attendance_query.where('class_id', '==', assigned_ids[0])
            else:
                attendance_query = attendance_query.where('class_id', 'in', assigned_ids)
        # For > 10, we retrieve all matching other filters and filter in Python below

    recent_attendance = attendance_query.order_by('-date').limit(200).all()
    if current_user.role == 'teacher' and len([str(x) for x in current_user.assigned_classes if x is not None]) > 10:
        assigned_set = set([str(x) for x in current_user.assigned_classes if x is not None])
        recent_attendance = [a for a in recent_attendance if str(getattr(a, 'class_id', '')) in assigned_set][:200]
    
    # Metadata for filters - Optimized fetching via cache
    all_subjects = get_cached_metadata('subjects', Subject)
    all_classes_objs = get_cached_metadata('classrooms', Classroom)
    
    if current_user.role == 'admin':
        all_classes = all_classes_objs
    else:
        assigned_ids = [str(x) for x in current_user.assigned_classes if x is not None]
        if not assigned_ids:
            all_classes = []
        else:
            all_classes = [c for c in all_classes_objs if str(getattr(c, 'id', '')) in assigned_ids]

            
    all_teachers = User.query.filter_by(role='teacher').all() 
    departments = [d.name for d in get_cached_metadata('departments', Department)]

    # Optimization: Perform total aggregation in a single query instead of O(N) queries
    # Fetch all relevant attendance records for the students being displayed
    rep_attendance_query = Attendance.query
    if subject_id: rep_attendance_query = rep_attendance_query.filter_by(subject_id=subject_id)
    if class_id: rep_attendance_query = rep_attendance_query.filter_by(class_id=class_id)
    
    relevant_attendance = rep_attendance_query.all()
    # student_id -> {date -> set of statuses}
    student_days_map = {}
    student_lates_map = {}
    
    for rec in relevant_attendance:
        sid = getattr(rec, 'student_id', None)
        if not sid: continue
        sid_str = str(sid)
        if sid_str not in student_days_map:
            student_days_map[sid_str] = {}
            student_lates_map[sid_str] = 0
        
        dt = getattr(rec, 'date', None)
        if not dt: continue
        
        if dt not in student_days_map[sid_str]:
            student_days_map[sid_str][dt] = set()
            
        st = getattr(rec, 'status', 'Present')
        student_days_map[sid_str][dt].add(st)
        if st == 'Late':
            student_lates_map[sid_str] += 1

    report_data = []
    for s in students:
        s_id_str = str(s.id)
        days = student_days_map.get(s_id_str, {})
        total_days = len(days)
    for s in students: # Iterate over the already filtered 'students' list
        s_id_str = str(s.id)
        days = student_days_map.get(s_id_str, {})
        total_days = len(days)
        
        present_count = 0
        absent_count = 0
        od_count = 0
        leave_count = 0
        
        for dt, statuses in days.items():
            if 'Absent' in statuses:
                absent_count += 1
            else:
                present_count += 1
                if 'OD' in statuses: od_count += 1
                elif 'Leave' in statuses: leave_count += 1
        
        total_lates_all = student_lates_map.get(s_id_str, 0)
        penalty = total_lates_all // 3
        effective_present = max(0, present_count - penalty)
        
        perc = min(100.0, (effective_present / total_days * 100)) if total_days > 0 else 0.0
        
        report_data.append({
            'student': s,
            'total': total_days,
            'present': effective_present,
            'absent': absent_count,
            'od': od_count,
            'leave': leave_count,
            'late': total_lates_all,
            'perc': round(perc, 2)
        })

    # Create mappings for manual relationship resolution in templates
    class_map = {str(c.id): c.name for c in all_classes_objs}
    subject_map = {str(s.id): s.name for s in all_subjects}
    teacher_map = {str(t.id): t.name for t in all_teachers}
    student_map = {str(s.id): s.name for s in students}

    return render_template('reports.html', 
                         report=report_data, 
                         subjects=all_subjects, 
                         classes=all_classes,
                         teachers=all_teachers,
                         departments=departments,
                         recent_attendance=recent_attendance,
                         class_map=class_map,
                         subject_map=subject_map,
                         teacher_map=teacher_map,
                         student_map=student_map)

@app.route('/export_excel')
@login_required
@teacher_allowed
def export_excel():
    subject_id = request.args.get('subject_id')
    dept = request.args.get('dept')
    start_date = request.args.get('start_date')
    end_date = request.args.get('end_date')

    # Fetch all data and filter in Python for simplicity in migration
    att_list = Attendance.query.all()
    results = []
    
    for att in att_list:
        # Apply filters
        if subject_id and str(att.subject_id) != str(subject_id): continue
        if start_date and att.date.date() < datetime.strptime(start_date, '%Y-%m-%d').date(): continue
        if end_date and att.date.date() > datetime.strptime(end_date, '%Y-%m-%d').date(): continue
        
        student = Student.query.get(att.student_id)
        if not student: continue
        if dept and student.dept != dept: continue
        
        subject = Subject.query.get(att.subject_id)
        
        results.append({
            'Student Name': student.name,
            'Roll No': student.roll_no,
            'Department': student.dept,
            'Subject': subject.name if subject else 'Unknown',
            'Date': att.date,
            'Status': att.status
        })

    if not results:
        flash("No data found for the selected criteria.", "warning")
        return redirect(url_for('reports'))


    # Create DataFrame
    df = pd.DataFrame(results)

    
    # Format Date
    if 'Date' in df.columns:
        df['Date'] = pd.to_datetime(df['Date']).dt.strftime('%d-%m-%Y')

    # Output to Excel
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Attendance Report')
    
    output.seek(0)
    
    return send_file(
        output,
        as_attachment=True,
        download_name=f"Attendance_Report_{datetime.now().strftime('%Y%m%d_%H%M%S')}.xlsx",
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

@app.route('/export_summary_excel')
@login_required
@teacher_allowed
def export_summary_excel():
    # Use the same logic as reports() to calculate the summary
    dept = request.args.get('dept', '')
    class_id = request.args.get('class_id', '')
    semester = request.args.get('semester', '')
    subject_id = request.args.get('subject_id', '')
    
    # 1. Filter Students
    all_students = Student.query.all()
    filtered_students = []
    for s in all_students:
        if dept and str(getattr(s, 'dept', '')).lower().strip() != dept.lower().strip(): continue
        if class_id and str(getattr(s, 'class_id', '')) != str(class_id): continue
        if semester and str(getattr(s, 'semester', '')) != str(semester): continue
        
        if current_user.role == 'teacher':
            assigned = [str(x) for x in current_user.assigned_classes if x is not None]
            if str(getattr(s, 'class_id', '')) not in assigned: continue
        filtered_students.append(s)
    
    if not filtered_students:
        flash("No students found for export.", "warning")
        return redirect(url_for('reports'))

    # 2. Fetch Attendance and Aggregate
    att_query = Attendance.query
    if subject_id: att_query = att_query.filter_by(subject_id=subject_id)
    if class_id: att_query = att_query.filter_by(class_id=class_id)
    
    relevant_attendance = att_query.all()
    # student_id -> {date -> set of statuses}
    student_days_map = {}
    student_lates_map = {}
    
    for rec in relevant_attendance:
        sid = str(getattr(rec, 'student_id', ''))
        if not sid: continue
        if sid not in student_days_map:
            student_days_map[sid] = {}
            student_lates_map[sid] = 0
        
        dt = getattr(rec, 'date', None)
        if not dt: continue
        
        if dt not in student_days_map[sid]:
            student_days_map[sid][dt] = set()
            
        st = getattr(rec, 'status', 'Present')
        student_days_map[sid][dt].add(st)
        if st == 'Late':
            student_lates_map[sid] += 1

    # 3. Build Results
    results = []
    for s in filtered_students:
        s_id_str = str(s.id)
        days = student_days_map.get(s_id_str, {})
        total_days = len(days)
        
        present_count = 0
        absent_count = 0
        od_count = 0
        leave_count = 0
        
        for dt, statuses in days.items():
            if 'Absent' in statuses:
                absent_count += 1
            else:
                present_count += 1
                if 'OD' in statuses: od_count += 1
                elif 'Leave' in statuses: leave_count += 1
        
        total_lates_all = student_lates_map.get(s_id_str, 0)
        penalty = total_lates_all // 3
        effective_present = max(0, present_count - penalty)
        
        perc = min(100.0, round((effective_present / total_days * 100), 2)) if total_days > 0 else 0.0
        
        results.append({
            'Roll No': s.roll_no,
            'Name': s.name,
            'Department': s.dept,
            'Total Days': total_days,
            'Present Days': effective_present,
            'Absent Days': absent_count,
            'OD': od_count,
            'Leave': leave_count,
            'Late': total_lates_all,
            'Penalty (Leaves)': penalty,
            'Final Percentage': f"{perc}%"
        })

    df = pd.DataFrame(results)
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        df.to_excel(writer, index=False, sheet_name='Summary')
    output.seek(0)
    
    return send_file(
        output,
        as_attachment=True,
        download_name=f"Attendance_Summary_{datetime.now().strftime('%Y%m%d')}.xlsx",
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet'
    )

# -- Student Portal --
@app.route('/student/dashboard')
@login_required
def student_dashboard():
    if current_user.role != 'student':
        return redirect(url_for('dashboard'))
    
    student = Student.query.get(current_user.student_id)
    if not student:
        flash('Student record not found.', 'danger')
        return redirect(url_for('dashboard'))
        
    # Optimized: Fetch all attendance records once (Robust: Check both ID and Roll No)
    recs_id = Attendance.query.filter_by(student_id=str(student.id)).all()
    recs_roll = Attendance.query.filter_by(student_id=str(student.roll_no)).all()
    all_attendance_map = {r.id: r for r in (recs_id + recs_roll)}
    all_attendance = list(all_attendance_map.values())
    # Filter subjects for current semester and department (Robust check)
    all_subjects = Subject.query.all()
    subjects = [
        s for s in all_subjects 
        if str(getattr(s, 'semester', '')).strip() == str(student.semester).strip() and
        str(getattr(s, 'dept', '')).lower().strip() == str(student.dept).lower().strip()
    ]
    
    # Fallback: if student has attendance for any subject, include it to ensure visibility
    attended_ids = {getattr(r, 'subject_id', '') for r in all_attendance if getattr(r, 'subject_id', '') != 'GLOBAL'}
    seen_ids = {s.id for s in subjects}
    for s in all_subjects:
        if s.id in attended_ids and s.id not in seen_ids:
            subjects.append(s)
        
    subject_stats = []
    total_held_overall = 0
    total_present_overall = 0
    total_od_overall = 0
    total_leave_overall = 0
    total_late_overall = 0
    
    # Pre-calculate global stats for the current student
    global_records = [r for r in all_attendance if getattr(r, 'subject_id', '') == 'GLOBAL']
    
    for sub in subjects:
        sub_id_str = str(sub.id).strip()
        subj_records = [r for r in all_attendance if str(getattr(r, 'subject_id', '')).strip() == sub_id_str]
        total = len(subj_records)
        
        if total == 0:
            p, od, leave, late, perc = 0, 0, 0, 0, 0.0
        else:
            p_raw = len([r for r in subj_records if getattr(r, 'status', '') == 'Present'])
            od_subj = len([r for r in subj_records if getattr(r, 'status', '') == 'OD'])
            leave_subj = len([r for r in subj_records if getattr(r, 'status', '') == 'Leave'])
            late_subj = len([r for r in subj_records if getattr(r, 'status', '') == 'Late'])
            
            # Penalize lates for this subject
            eff_p = max(0, (p_raw + od_subj + leave_subj + late_subj) - (late_subj // 3))
            
            perc = min(100.0, round((eff_p / total * 100), 2))
            p = eff_p
            od = od_subj
            leave = leave_subj
            late = late_subj

        subject_stats.append({
            'name': sub.name,
            'code': sub.code,
            'total': total,
            'present': p,
            'perc': perc,
            'od': od,
            'leave': leave,
            'late': late
        })
        total_held_overall += total
        total_present_overall += p

    # Add row for Global records if any exist and are not already counted
    if global_records:
        g_p = len([r for r in global_records if getattr(r, 'status', '') == 'Present'])
        g_od = len([r for r in global_records if getattr(r, 'status', '') == 'OD'])
        g_leave = len([r for r in global_records if getattr(r, 'status', '') == 'Leave'])
        g_late = len([r for r in global_records if getattr(r, 'status', '') == 'Late'])
        g_total = len(global_records)
        g_eff = max(0, (g_p + g_od + g_leave + g_late) - (g_late // 3))
        
        subject_stats.append({
            'name': 'General/Gate Marking',
            'code': 'GLOBAL',
            'total': g_total,
            'present': g_eff,
            'perc': round((g_eff / g_total * 100), 2) if g_total > 0 else 0,
            'od': g_od,
            'leave': g_leave,
            'late': g_late
        })
        total_held_overall += g_total
        total_present_overall += g_eff

    # Get overall stats using centralized logic (day-wise + penalties)
    overall_res = student.get_attendance_stats()
    
    return render_template('student_dashboard.html', 
                           student=student,
                           subject_stats=subject_stats,
                           total_held=overall_res[0],
                           total_present=overall_res[1],
                           total_absent=overall_res[0] - overall_res[1], # Consistent with day-wise
                           total_od=overall_res[3],
                           total_leave=overall_res[4],
                           total_late=overall_res[5],
                           overall_perc=overall_res[2])
    
@app.route('/calculator')
@login_required
def attendance_calculator():
    if current_user.role == 'student':
        student = Student.query.get(current_user.student_id)
        if not student:
            flash('Student record not found.', 'danger')
            return redirect(url_for('dashboard'))
        
        # Get subjects for the current semester and department (Robust check)
        subjects_all = Subject.query.filter_by(semester=str(student.semester)).all()
        if not subjects_all:
            try:
                subjects_all = Subject.query.filter_by(semester=int(student.semester)).all()
            except:
                subjects_all = []
                
        subjects = [
            s for s in subjects_all 
            if str(getattr(s, 'dept', '')).lower().strip() == student.dept.lower().strip()
        ]
            
        # Use the centralized model method for consistency
        stats = student.get_attendance_stats()
        
        return render_template('calculator.html', 
                               student=student, 
                               stats=stats)
    else:
        # For teachers/admins, show a selector or handle selected student
        student_id = request.args.get('student_id')
        student = None
        stats = None
        if student_id:
            student = Student.query.get(student_id)
            if student:
                # Teachers still see cumulative stats for simplicity, or we could filter here too
                stats = student.get_attendance_stats()
        
        all_students = Student.query.all()
        # Sort students by roll no for better selector usability
        all_students.sort(key=lambda x: x.roll_no.lower())
            
        return render_template('calculator.html', 
                               all_students=all_students, 
                               selected_student=student,
                               stats=stats)

# -- Department Management (Admin Only) --
@app.route('/departments', methods=['GET', 'POST'])
@login_required
@admin_required
def departments():
    if request.method == 'POST':
        name = request.form.get('name')
        code = request.form.get('code')
        
        existing_dept = Department.query.filter_by(name=name).first()
        if existing_dept:
            flash('Department already exists!', 'danger')
        else:
            new_dept = Department(name=name, code=code)
            new_dept.save()
            flash(f'Department {name} added successfully!', 'success')
        return redirect(url_for('departments'))
    
    all_depts = Department.query.all()
    # Sort departments by name
    all_depts.sort(key=lambda x: x.name.lower())
    return render_template('departments.html', departments=all_depts)

@app.route('/delete_department/<id>')
@login_required
@admin_required
def delete_department(id):
    dept = Department.query.get_or_404(id)
    if dept:
        dept.delete()
        flash('Department deleted!', 'info')
    return redirect(url_for('departments'))

@app.route('/edit_department/<id>', methods=['POST'])
@login_required
@admin_required
def edit_department(id):
    try:
        dept = Department.query.get_or_404(id)
        dept.update(
            name=request.form.get('name'),
            code=request.form.get('code')
        )
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'success', 'message': 'Department updated!'})
        flash('Department updated!', 'success')
    except Exception as e:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'status': 'error', 'message': str(e)}), 500
        flash(f'Error updating department: {str(e)}', 'danger')
    return redirect(url_for('departments'))

# --- Security Management (Admin Only) ---
@app.route('/manage_security', methods=['GET', 'POST'])
@login_required
@admin_required
def manage_security():
    if request.method == 'POST':
        name = request.form.get('name')
        email = request.form.get('email')
        password = request.form.get('password')
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already exists!', 'danger')
        else:
            phone = request.form.get('phone')
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            new_security = User(name=name, email=email, username=email, password=hashed_pw, role='security', phone=phone)
            new_security.save()
            flash(f'Security Personal {name} added successfully!', 'success')
        return redirect(url_for('manage_security'))
    
    all_security = User.query.filter_by(role='security').all()
    # Sort security by name
    all_security.sort(key=lambda x: x.name.lower())
    return render_template('manage_security.html', security_users=all_security)

@app.route('/delete_security/<id>')
@login_required
@admin_required
def delete_security(id):
    user = User.query.get_or_404(id)
    if user and user.role == 'security':
        user.delete()
        flash('Security user deleted!', 'info')
    return redirect(url_for('manage_security'))

@app.route('/portal/<status_type>', methods=['GET', 'POST'])
@login_required
def status_portal(status_type):
    # Normalize status type for consistency
    status_type = status_type.upper()
    if status_type not in ['OD', 'LEAVE', 'LATE']:
        # Support legacy ML slug just in case but handle as LEAVE
        if status_type == 'ML':
            return redirect(url_for('status_portal', status_type='LEAVE', **request.args))
        abort(404)
    # Check permissions
    allowed_roles = ['admin', 'hod', 'teacher', 'in_charge']
    if status_type == 'LATE':
        allowed_roles.append('security')
        
    if current_user.role not in allowed_roles:
        flash('Unauthorized Access!', 'danger')
        return redirect(url_for('dashboard'))

    search_query = request.args.get('search', '')
    dept_filter = request.args.get('dept', '')
    class_filter = request.args.get('class_id', '')
    selected_date = request.args.get('date', datetime.utcnow().strftime('%Y-%m-%d'))
    
    # Metadata fetching
    all_departments = get_cached_metadata('departments', Department)
    all_classrooms = get_cached_metadata('classrooms', Classroom)
    
    # Filter metadata for teachers/in-charges
    assigned_class_ids = []
    if current_user.role in ['teacher', 'in_charge']:
        assigned_class_ids = [str(x) for x in getattr(current_user, 'assigned_classes', []) if x]
        classrooms = [c for c in all_classrooms if str(c.id) in assigned_class_ids]
        assigned_depts = {str(getattr(c, 'dept', '')).lower().strip() for c in classrooms}
        departments = [d for d in all_departments if str(getattr(d, 'name', '')).lower().strip() in assigned_depts]
    else:
        classrooms = all_classrooms
        departments = all_departments
    
    # Capitalize status for display (LEAVE -> Leave, OD -> OD, LATE -> Late)
    display_status = status_type.title() if status_type in ['LATE', 'LEAVE'] else status_type
    
    # Fetch already marked students for this date and status type
    marked_records = Attendance.query.filter_by(date=selected_date, status=display_status).all()
    marked_student_ids = [str(getattr(r, 'student_id', '')) for r in marked_records]
    
    # Fetch all students and filter in Python for robustness (handling case/whitespace)
    students = []
    
    # Show results if any filter is applied OR show already marked students by default
    if search_query or dept_filter or class_filter:
        # Optimization: If class_filter is present, only fetch students of that class
        if class_filter:
            # Security: If teacher, ensure they only search their assigned classes
            if assigned_class_ids and str(class_filter) not in assigned_class_ids:
                pool = []
            else:
                pool = Student.query.filter_by(class_id=class_filter).all()
        elif assigned_class_ids:
            # Teacher searching globally - restrict pool to their assigned classes
            # Use query if under 30 classes, else filter in Python
            if len(assigned_class_ids) <= 30:
                pool = Student.query.where('class_id', 'in', assigned_class_ids).all()
            else:
                pool = [s for s in Student.query.all() if str(getattr(s, 'class_id', '')) in assigned_class_ids]
        else:
            pool = Student.query.all()
            
        for s in pool:
            # Dept Filter (case-insensitive and trimmed)
            if dept_filter:
                s_dept = str(getattr(s, 'dept', '')).lower().strip()
                if s_dept != dept_filter.lower().strip():
                    continue
            
            # Search Query (Name or Roll No)
            if search_query:
                query_l = search_query.lower()
                s_name = str(getattr(s, 'name', '')).lower()
                s_roll = str(getattr(s, 'roll_no', '')).lower()
                if query_l not in s_name and query_l not in s_roll:
                    continue
            
            students.append(s)
    else:
        # Default view: show already marked students
        if marked_student_ids:
            all_s = Student.query.all()
            for s in all_s:
                sid = str(s.id)
                if sid in marked_student_ids:
                    # Filter for teachers: only show their assigned students in default view too
                    s_class_id = str(getattr(s, 'class_id', ''))
                    if assigned_class_ids and s_class_id not in assigned_class_ids:
                        continue
                    students.append(s)
    
    # Sort students: Dept wise then by roll no
    students.sort(key=lambda x: (str(getattr(x, 'dept', '')).lower(), str(getattr(x, 'roll_no', 'zzzz')).lower()))

    # Fetch history logs (Last 30 Days)
    from datetime import timedelta
    one_month_ago = (datetime.utcnow() - timedelta(days=30)).strftime('%Y-%m-%d')
    db_status = display_status
    
    history_records = Attendance.query.filter_by(status=db_status).where('date', '>=', one_month_ago).all()
    history_records.sort(key=lambda x: getattr(x, 'date', ''), reverse=True)
    
    # Resolve student info for history
    history_data = []
    # Pre-fetch students and classes to avoid N queries
    all_students_map = {str(s.id): s for s in Student.query.all()}
    all_classes_map = {str(c.id): getattr(c, 'name', 'Unknown') for c in all_classrooms}
    
    for rec in history_records:
        sid = str(getattr(rec, 'student_id', ''))
        student = all_students_map.get(sid)
        if student:
            # Filter history for teachers: only show records of their assigned classes
            s_class_id = str(getattr(student, 'class_id', ''))
            if assigned_class_ids and s_class_id not in assigned_class_ids:
                continue
                
            history_data.append({
                'name': student.name,
                'roll_no': student.roll_no,
                'dept': student.dept,
                'class_name': all_classes_map.get(s_class_id, 'Unknown'),
                'date': rec.date,
                'id': rec.id
            })

    return render_template('status_portal.html', 
                         students=students, 
                         search_query=search_query, 
                         status_type=status_type,
                         display_status=display_status,
                         departments=departments,
                         classrooms=classrooms,
                         dept_filter=dept_filter,
                         class_filter=class_filter,
                         selected_date=selected_date,
                         marked_student_ids=marked_student_ids,
                         history_data=history_data[:100]) # Limit to 100 recent for performance

@app.route('/mark_status_global/<status_type>/<student_id>', methods=['POST'])
@login_required
def mark_status_global(status_type, student_id):
    status_type = status_type.upper()
    if status_type not in ['OD', 'LEAVE', 'LATE']:
        if status_type == 'ML':
            status_type = 'LEAVE'
        else:
            abort(404)
        
    # Check permissions
    allowed_roles = ['admin', 'hod', 'teacher', 'in_charge']
    if status_type == 'LATE':
        allowed_roles.append('security')
        
    if current_user.role not in allowed_roles:
        abort(403)

    student = Student.query.get_or_404(student_id)
    
    # Get configuration search/filter params for redirect back
    search = request.args.get('search', '')
    dept = request.args.get('dept', '')
    class_id = request.args.get('class_id', '')
    date_str = request.args.get('date') or datetime.utcnow().strftime('%Y-%m-%d')
    
    # Ensure status is properly capitalized for database (Late, OD, Leave)
    db_status = status_type.title() if status_type in ['LATE', 'LEAVE'] else status_type
    
    # Logic: Mark for the selected date as a GLOBAL record (Subject Independent)
    existing = Attendance.query.filter_by(
        student_id=student.id, 
        subject_id='GLOBAL', 
        date=date_str
    ).first()
    
    if existing:
        existing.status = db_status
        existing.save()
    else:
        new_rec = Attendance(
            student_id=student.id,
            subject_id='GLOBAL',
            class_id=getattr(student, 'class_id', 'Unknown'),
            teacher_id=current_user.id,
            date=date_str,
            status=db_status
        )
        new_rec.save()
        
    flash(f'Marked {student.name} as {db_status} for {date_str} (Global Entry)!', 'success')
    return redirect(url_for('status_portal', 
                          status_type=status_type, 
                          search=search, 
                          dept=dept, 
                          class_id=class_id, 
                          date=date_str))

@app.route('/security_portal', methods=['GET', 'POST'])
@login_required
@security_required
def security_portal():
    return redirect(url_for('status_portal', status_type='LATE', search=request.args.get('search', '')))

@app.route('/security_mark_late/<student_id>', methods=['POST'])
@login_required
@security_required
def security_mark_late(student_id):
    return redirect(url_for('mark_status_global', status_type='LATE', student_id=student_id, search=request.args.get('search', '')))

if __name__ == '__main__':
    app.run(debug=True, port=5001)
