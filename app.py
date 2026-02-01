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
            db.session.add(admin)
            db.session.commit()
            print("Default admin created.")
    except Exception as e:
        print(f"Error creating default admin: {e}")

# Create database and default admin (HOD)
with app.app_context():
    db.create_all()
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
    # Use filter_by for 'in' queries (passing a list triggers 'in')
    total_teachers = User.query.filter_by(role=['teacher', 'in_charge', 'hod']).count()
    
    # Simple stats for dashboard
    students = Student.query.all()
    report_data = []
    for s in students:
        total, present, perc = s.get_attendance_stats()
        report_data.append({
            'name': s.name,
            'roll_no': s.roll_no,
            'dept': s.dept,
            'perc': perc
        })
    
    today = datetime.utcnow().date()
    in_charge_data = None
    hod_summary = []

    if current_user.role == 'in_charge':
        cls = Classroom.query.filter_by(name=current_user.assigned_class).first()
        if cls:
            present_today = Attendance.query.filter_by(
                class_id=cls.id,
                date=datetime.combine(today, datetime.min.time()),
                status='Present'
            ).count()
            absent_today = Attendance.query.filter_by(
                class_id=cls.id,
                date=datetime.combine(today, datetime.min.time()),
                status='Absent'
            ).count()

            in_charge_data = {'present': present_today, 'absent': absent_today, 'class_name': cls.name}
    
    # HOD/Admin see summary for all classes, Teachers see summary for their assigned class
    if current_user.role in ['hod', 'admin', 'teacher']:
        if current_user.role == 'teacher':
            # Teachers only see their assigned class
            classes_to_show = Classroom.query.filter_by(name=current_user.assigned_class).all() if current_user.assigned_class else []
        else:
            # HOD/Admin see everything
            classes_to_show = Classroom.query.all()

        for cls in classes_to_show:
            p = Attendance.query.filter_by(
                class_id=cls.id, 
                date=datetime.combine(today, datetime.min.time()), 
                status='Present'
            ).count()
            a = Attendance.query.filter_by(
                class_id=cls.id, 
                date=datetime.combine(today, datetime.min.time()), 
                status='Absent'
            ).count()

            hod_summary.append({'class_name': cls.name, 'present': p, 'absent': a})

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
        
        if User.query.filter_by(email=email).first():
            flash('Email already exists!', 'danger')
        else:
            phone = request.form.get('phone')
            hashed_pw = generate_password_hash(password, method='pbkdf2:sha256')
            new_teacher = User(name=name, email=email, username=email, password=hashed_pw, role='teacher', phone=phone)
            db.session.add(new_teacher)
            db.session.commit()
            flash(f'Teacher {name} added successfully!', 'success')
        return redirect(url_for('teachers'))
    
    all_teachers = User.query.filter_by(role='teacher').all()
    return render_template('teachers.html', teachers=all_teachers)

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
        db.session.add(new_class)
        db.session.commit()
        flash(f'Class {name} added!', 'success')
        return redirect(url_for('classes'))
    all_classes = Classroom.query.all()
    return render_template('classes.html', classes=all_classes)

@app.route('/delete_class/<id>')

@login_required
@admin_required
def delete_class(id):
    cls = Classroom.query.get_or_404(id)
    db.session.delete(cls)
    db.session.commit()
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
        db.session.rollback()
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
    if teacher.role == 'teacher':
        db.session.delete(teacher)
        db.session.commit()
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
    
    # Update assignments
    teacher.assigned_classes = []
    for cid in class_ids:
        cls = Classroom.query.get(cid)
        if cls:
            teacher.assigned_classes.append(cls)
    
    db.session.add(teacher)
    db.session.commit()
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
        'assigned_classes': [c.id for c in teacher.assigned_classes]
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
        db.session.add(user)
        db.session.commit()
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
        db.session.add(new_student)
        
        # Create User Login for Student
        default_pw = generate_password_hash('student123', method='pbkdf2:sha256')
        new_user = User(username=roll_no, password=default_pw, role='student', student_id=new_student.id, phone=phone)
        db.session.add(new_user)
        
        db.session.commit()

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
    # Also delete the associated user account
    user = User.query.filter_by(student_id=student.id).first()
    if user:
        db.session.delete(user)
    db.session.delete(student)
    db.session.commit()
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
            
            success_count = 0
            skipped_count = 0
            for _, row in df.iterrows():
                name = str(row['Name']).strip()
                roll_no = str(row['Roll No']).strip()
                dept = str(row['Dept']).strip()
                email = str(row.get('Email', '')).strip()
                phone = str(row.get('Phone', '')) if 'Phone' in df.columns else None
                class_name = str(row.get('Class_Name', '')).strip()
                
                # Basic validation
                if roll_no == 'nan' or name == 'nan':
                    skipped_count += 1
                    print(f"Skipping row due to missing Name or Roll No: {row.to_dict()}")
                    continue

                class_id = None
                semester = 1
                cls = Classroom.query.filter_by(name=class_name).first()
                if cls:
                    class_id = cls.id
                    semester = cls.current_semester
                else:
                    # If class doesn't exist, we might want to skip or create it
                    # For now, let's skip to avoid orphan students
                    print(f"Skipping {name}: Class '{class_name}' not found")
                    skipped_count += 1
                    continue

                # Check if Student already exists
                if Student.query.filter_by(roll_no=roll_no).first():
                    print(f"Skipping {name}: Student record with Roll No {roll_no} already exists")
                    skipped_count += 1
                    continue 

                try:
                    # Create new student record
                    new_student = Student(name=name, roll_no=roll_no, dept=dept, phone=phone, semester=semester, class_id=class_id)
                    db.session.add(new_student)



                    
                    # Check if User already exists (orphaned account)
                    existing_user = User.query.filter_by(username=roll_no).first()
                    if existing_user:
                        # Link the existing user to the new student
                        existing_user.student_id = new_student.id
                        existing_user.name = name
                        if phone: existing_user.phone = phone
                        print(f"Linked existing user {roll_no} to new student {name}")
                    else:
                        # Create new user login
                        default_pw = generate_password_hash('student123', method='pbkdf2:sha256')
                        new_user = User(name=name, email=email if email else None, username=roll_no, 
                                        password=default_pw, role='student', student_id=new_student.id, phone=phone)
                        db.session.add(new_user)
                        print(f"Created new user for student {name}")
                        
                    success_count += 1
                except Exception as e:
                    db.session.rollback()
                    print(f"Error inserting student {name} (Roll No: {roll_no}): {str(e)}")
                    skipped_count += 1
            
            db.session.commit()
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
        db.session.add(new_subject)
        db.session.commit()
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
        db.session.delete(subject)
        db.session.commit()
        flash('Subject deleted!', 'info')
    except Exception as e:
        db.session.rollback()
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
    
    db.session.commit()
    
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
        all_classes = current_user.assigned_classes.all()

    if request.method == 'POST':
        subject_id = request.form.get('subject_id')
        class_id = request.form.get('class_id')
        subject = Subject.query.get(subject_id)
        if not subject or not class_id:
            flash('Invalid Subject or Class', 'danger')
            return redirect(url_for('attendance'))
            
        date_str = request.form.get('date')
        date_obj = datetime.strptime(date_str, '%Y-%m-%d').date()
        
        # Fetch students for the SELECTED CLASS only
        students = Student.query.filter_by(class_id=class_id).all()
        
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
                db.session.add(new_record)

        
        db.session.commit()
        flash('Attendance marked successfully!', 'success')
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
        assigned_ids = [c.id for c in current_user.assigned_classes]
        student_query = student_query.filter_by(class_id=assigned_ids)
    
    students = student_query.all()
    
    # Base Attendance Query for Logs
    attendance_query = Attendance.query
    if subject_id: attendance_query = attendance_query.filter_by(subject_id=subject_id)
    if teacher_id: attendance_query = attendance_query.filter_by(teacher_id=teacher_id)
    if start_date: 
        s_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        attendance_query = attendance_query.where('date', '>=', s_date)
    if end_date:
        e_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        attendance_query = attendance_query.where('date', '<=', e_date)
    
    if current_user.role == 'teacher':
        # Limit to assigned classes' students
        assigned_ids = [c.id for c in current_user.assigned_classes]
        attendance_query = attendance_query.filter_by(class_id=assigned_ids) # Or handle in Python if needed


    recent_attendance = attendance_query.order_by('-date').limit(200).all()
    
    # Metadata for filters
    all_subjects = Subject.query.all()
    all_classes = Classroom.query.all() if current_user.role == 'admin' else current_user.assigned_classes.all()
    all_teachers = User.query.filter_by(role='teacher').all()
    departments = list(set([c.dept for c in Classroom.query.all()]))


    report_data = []
    for s in students:
        t, p, perc = s.get_attendance_stats(subject_id=subject_id)
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
