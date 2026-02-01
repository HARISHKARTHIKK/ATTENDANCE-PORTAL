import os
import json
import firebase_admin
from firebase_admin import credentials, firestore
from dotenv import load_dotenv

# 1. Initialize Firebase Admin SDK AT THE VERY TOP
load_dotenv()
firebase_key_raw = os.getenv('FIREBASE_KEY')

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
        else:
            # Fallback for local development
            firebase_admin.initialize_app()
            print("Firebase initialized with default credentials")
    except Exception as e:
        print(f"CRITICAL: Firebase initialization failed: {e}")

from flask import Flask, render_template, request, redirect, url_for, flash, send_file, jsonify, session, abort
from flask_login import LoginManager, login_user, logout_user, login_required, current_user
import pandas as pd
from io import BytesIO
from werkzeug.security import generate_password_hash, check_password_hash
from models import db, User, Student, Subject, Attendance, Classroom, Department
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

# --- Routes ---
@app.route('/health')
def health():
    return "OK", 200


@app.route('/')
@login_required
def dashboard():
    if current_user.role == 'student':
        return redirect(url_for('student_dashboard'))
    
    total_students = Student.query.count()
    total_subjects = Subject.query.count()
    total_classes = Classroom.query.count()
    # Use where() for 'in' queries as per requirement
    total_teachers = User.query.where('role', 'in', ['teacher', 'in_charge', 'hod']).count()
    
    # Limit students to top 10 for Quick Summary on initial load to save N queries
    # Users can see full data on the Reports page.
    students = Student.query.all()
    top_students = students[:10]
    report_data = []
    for s in top_students:
        total, present, perc = s.get_attendance_stats()
        report_data.append({
            'name': s.name,
            'roll_no': s.roll_no,
            'dept': s.dept,
            'perc': perc
        })
    
    today = datetime.utcnow().date()
    today_dt = datetime.combine(today, datetime.min.time())
    in_charge_data = None
    hod_summary = []

    # Optimization: Pre-fetch today's attendance records to avoid O(C) queries
    attendance_today = Attendance.query.filter_by(date=today_dt).all()
    stats_map = {}
    for rec in attendance_today:
        cid = getattr(rec, 'class_id', None)
        if not cid: continue
        if cid not in stats_map:
            stats_map[cid] = {'present': 0, 'absent': 0}
        if rec.status == 'Present':
            stats_map[cid]['present'] += 1
        elif rec.status == 'Absent':
            stats_map[cid]['absent'] += 1

    if current_user.role == 'in_charge':
        cls = Classroom.query.where('id', 'in', current_user.assigned_classes).first() if current_user.assigned_classes else None
        if cls:
            stats = stats_map.get(cls.id, {'present': 0, 'absent': 0})
            in_charge_data = {'present': stats['present'], 'absent': stats['absent'], 'class_name': cls.name}
    
    if current_user.role in ['hod', 'admin', 'teacher']:
        if current_user.role == 'teacher':
            classes_to_show = Classroom.query.where('id', 'in', current_user.assigned_classes).all() if current_user.assigned_classes else []
        else:
            classes_to_show = Classroom.query.all()

        for cls in classes_to_show:
            stats = stats_map.get(cls.id, {'present': 0, 'absent': 0})
            hod_summary.append({
                'class_name': cls.name, 
                'present': stats['present'], 
                'absent': stats['absent']
            })

    return render_template('dashboard.html', 
                           total_students=total_students, 
                           total_subjects=total_subjects,
                           total_classes=total_classes,
                           total_teachers=total_teachers,
                           report_data=report_data,
                           in_charge_data=in_charge_data,
                           hod_summary=hod_summary,
                           today_date=today.strftime('%d %b, %Y'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form.get('username')
        password = request.form.get('password')
        
        # 1. Check for missing fields
        if not username or not password:
            flash('Please provide both username and password', 'warning')
            return render_template('login.html')

        try:
            # 2. Fetch user safely
            user = User.query.filter_by(username=username).first()
            
            # 3. Check existence and password safely
            if user and check_password_hash(getattr(user, 'password', ''), password):
                login_user(user)
                
                # 4. Explicit session handling as requested
                session['user_id'] = user.id
                session.permanent = True
                
                flash(f'Welcome back, {user.name}!', 'success')
                
                if user.role in ['admin', 'teacher', 'hod', 'in_charge']:
                    return redirect(url_for('dashboard'))
                return redirect(url_for('student_dashboard'))
            else:
                flash('Invalid username or password', 'danger')
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
        password = request.form.get('password')
        
        existing_user = User.query.filter_by(email=email).first()
        if existing_user:
            flash('Email already exists!', 'danger')
        else:
            phone = request.form.get('phone')
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            new_teacher = User(name=name, email=email, username=email, password=hashed_pw, role='teacher', phone=phone)
            new_teacher.save()
            flash(f'Teacher {name} added successfully!', 'success')
        return redirect(url_for('teachers'))
    
    all_teachers = User.query.filter_by(role='teacher').all()
    all_classes = Classroom.query.all()
    class_map = {str(c.id): c.name for c in all_classes}
    return render_template('teachers.html', teachers=all_teachers, class_map=class_map)

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
    return render_template('classes.html', classes=all_classes)

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
    subjects = Subject.query.filter_by(semester=semester_id, dept=dept).all()
    return jsonify([{
        'id': s.id,
        'name': s.name,
        'code': s.code
    } for s in subjects])

@app.route('/delete_teacher/<id>')

@login_required
@admin_required
def delete_teacher(id):
    teacher = User.query.get_or_404(id)
    if teacher and teacher.role == 'teacher':
        teacher.delete()
        flash('Teacher removed!', 'info')
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
        'phone': request.form.get('phone')
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
        'email': teacher.email,
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
        user.email = request.form.get('email')
        user.phone = request.form.get('phone')
        if request.form.get('password'):
            user.password = generate_password_hash(request.form.get('password'), method='pbkdf2:sha256')
        user.save()
        return jsonify({'status': 'success', 'message': 'Profile updated!'})
    
    return jsonify({
        'name': current_user.name,
        'email': current_user.email,
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
        new_user = User(username=roll_no, password=default_pw, role='student', student_id=new_student.id, phone=phone)
        new_user.save()

        flash(f'Student {name} added! Login: {roll_no} / student123', 'success')
        return redirect(url_for('students'))
    
    all_students = Student.query.all()
    all_classes = Classroom.query.all()
    return render_template('students.html', students=all_students, classes=all_classes)

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
                stream = io.StringIO(file.stream.read().decode("UTF8"), newline=None)
                df = pd.read_csv(stream)
            else:
                df = pd.read_excel(file)
            
            # Required columns: Name, Roll No, Dept, Class_Name
            required_cols = ['Name', 'Roll No', 'Dept', 'Class_Name']
            for col in required_cols:
                if col not in df.columns:
                    flash(f'Missing required column: {col}', 'danger')
                    return redirect(url_for('students'))
            
            # Pre-fetch for performance (O(1) lookups instead of O(N) queries)
            all_classes = {c.name: c for c in Classroom.query.all()}
            existing_roll_nos = {s.roll_no for s in Student.query.all()}
            existing_users = {u.username: u for u in User.query.all()}
            
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
                
                if roll_no == 'nan' or name == 'nan' or roll_no in existing_roll_nos:
                    skipped_count += 1
                    continue

                cls = all_classes.get(class_name)
                if not cls:
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
                            id=user_ref.id,
                            name=name, 
                            email=email if email else None, 
                            username=roll_no, 
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
                    print(f"Error processing {name}: {e}")
                    skipped_count += 1
            
            if batch_ops > 0:
                batch.commit()
            
            if success_count > 0:
                flash(f'Successfully imported {success_count} students!', 'success')
            if skipped_count > 0:
                flash(f'Skipped {skipped_count} students (duplicates or invalid data).', 'warning')
        except Exception as e:
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
    
    selected_semester = request.args.get('semester', type=int)
    query = Subject.query
    if selected_semester:
        query = query.filter_by(semester=selected_semester)
    
    all_subjects = query.all()
    all_teachers = User.query.filter_by(role='teacher').all()
    departments = list(set([c.dept for c in Classroom.query.all()]))

    if not departments:
        departments = ['Computer Science', 'Electronics', 'Mechanical', 'Civil']
        
    return render_template('subjects.html', subjects=all_subjects, teachers=all_teachers, 
                          selected_semester=selected_semester, departments=departments)

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
        # Guard against empty current_user.assigned_classes list for Firestore 'in' query
        if current_user.assigned_classes:
            all_classes = Classroom.query.where('id', 'in', current_user.assigned_classes).all()
        else:
            all_classes = []

    if request.method == 'POST':
        subject_id = request.form.get('subject_id')
        class_id = request.form.get('class_id')
        subject = Subject.query.get(subject_id)
        if not subject or not class_id:
            flash('Invalid Subject or Class', 'danger')
            return redirect(url_for('attendance'))
            
        date_str = request.form.get('date')
        # Consistently use datetime objects for Firestore Timestamp compatibility
        date_obj = datetime.strptime(date_str, '%Y-%m-%d')
        
        # Fetch students for the SELECTED CLASS only
        students = Student.query.filter_by(class_id=class_id).all()
        
        # Use Firestore Batch Write for performance
        db_conn = get_db()
        batch = db_conn.batch()
        count = 0
        
        for student in students:
            status = request.form.get(f'status_{student.id}')
            if status: 
                new_record = Attendance(
                    student_id=student.id,
                    subject_id=subject_id,
                    class_id=class_id,
                    teacher_id=current_user.id,
                    date=date_obj,
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

    # Base Student Query
    student_query = Student.query
    if dept: student_query = student_query.filter_by(dept=dept)
    if class_id: student_query = student_query.filter_by(class_id=class_id)
    if semester: student_query = student_query.filter_by(semester=semester)
    
    # Teachers can only see reports for their assigned classes
    if current_user.role == 'teacher':
        assigned_ids = current_user.assigned_classes
        if assigned_ids:
            student_query = student_query.where('class_id', 'in', assigned_ids)
        else:
            student_query = student_query.where('class_id', '==', '__none__')
    
    students = student_query.all()
    
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
        assigned_ids = current_user.assigned_classes
        if assigned_ids:
            attendance_query = attendance_query.where('class_id', 'in', assigned_ids)
        else:
            attendance_query = attendance_query.where('class_id', '==', '__none__')


    recent_attendance = attendance_query.order_by('-date').limit(200).all()
    
    # Metadata for filters - Optimized fetching
    all_subjects = Subject.query.all()
    all_classes_objs = Classroom.query.all()
    
    if current_user.role == 'admin':
        all_classes = all_classes_objs
    else:
        # Guard for 'in' query in metadata
        if current_user.assigned_classes:
            all_classes = Classroom.query.where('id', 'in', current_user.assigned_classes).all()
        else:
            all_classes = []
            
    all_teachers = User.query.filter_by(role='teacher').all()
    departments = list(set([c.dept for c in all_classes_objs]))

    # Optimization: Perform total aggregation in a single query instead of O(N) queries
    # Fetch all relevant attendance records for the students being displayed
    rep_attendance_query = Attendance.query
    if subject_id: rep_attendance_query = rep_attendance_query.filter_by(subject_id=subject_id)
    if class_id: rep_attendance_query = rep_attendance_query.filter_by(class_id=class_id)
    
    relevant_attendance = rep_attendance_query.all()
    attendance_map = {}
    for rec in relevant_attendance:
        sid = getattr(rec, 'student_id', None)
        if not sid: continue
        if sid not in attendance_map:
            attendance_map[sid] = {'total': 0, 'present': 0}
        attendance_map[sid]['total'] += 1
        if getattr(rec, 'status', '') == 'Present':
            attendance_map[sid]['present'] += 1

    report_data = []
    for s in students:
        stats = attendance_map.get(s.id, {'total': 0, 'present': 0})
        t = stats['total']
        p = stats['present']
        perc = round((p / t * 100), 2) if t > 0 else 0.0
        report_data.append({'student': s, 'total': t, 'present': p, 'percentage': perc})

    return render_template('reports.html', 
                         report=report_data, 
                         subjects=all_subjects, 
                         classes=all_classes,
                         teachers=all_teachers,
                         departments=departments,
                         recent_attendance=recent_attendance)

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
        
    subjects = Subject.query.all()
    
    subject_stats = []
    total_held_overall = 0
    total_present_overall = 0
    
    for sub in subjects:
        total, present, perc = student.get_attendance_stats(subject_id=sub.id)
        subject_stats.append({
            'name': sub.name,
            'code': sub.code,
            'total': total,
            'present': present,
            'perc': perc
        })
        total_held_overall += total
        total_present_overall += present
    
    overall_perc = (total_present_overall / total_held_overall * 100) if total_held_overall > 0 else 0
    absent_count_overall = total_held_overall - total_present_overall
    
    return render_template('student_dashboard.html', 
                           student=student,
                           subject_stats=subject_stats,
                           total_held=total_held_overall,
                           total_present=total_present_overall,
                           total_absent=absent_count_overall,
                           overall_perc=round(overall_perc, 2))

if __name__ == '__main__':
    app.run(debug=True, port=5001)
