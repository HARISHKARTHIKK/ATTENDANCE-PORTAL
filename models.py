from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()

# Association table for Teacher <-> Class mapping
teacher_classes = db.Table('teacher_classes',
    db.Column('teacher_id', db.Integer, db.ForeignKey('user.id'), primary_key=True),
    db.Column('class_id', db.Integer, db.ForeignKey('classroom.id'), primary_key=True)
)

class Department(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), unique=True, nullable=False)
    classes = db.relationship('Classroom', backref='department', lazy=True)

class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=True) # Full Name
    email = db.Column(db.String(120), unique=True, nullable=True) # Official Email
    username = db.Column(db.String(100), unique=True, nullable=False)
    password = db.Column(db.String(200), nullable=False)
    role = db.Column(db.String(20), default='admin') # 'admin', 'teacher', 'student'
    phone = db.Column(db.String(15), nullable=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=True)
    
    # Mapping to classes (for teachers)
    assigned_classes = db.relationship('Classroom', secondary=teacher_classes, 
                                     backref=db.backref('teachers', lazy='dynamic'), lazy='dynamic')
    
    # Relationship for teachers to their marked attendance records
    marked_attendance = db.relationship('Attendance', backref='marked_by', lazy=True)

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        db.session.commit()

class Classroom(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False) # e.g. III-CSE-B
    dept_id = db.Column(db.Integer, db.ForeignKey('department.id'), nullable=True)
    dept = db.Column(db.String(100), nullable=False) # Keeping for backwards compatibility
    current_semester = db.Column(db.Integer, default=1) # Current sem for this class
    year = db.Column(db.Integer, nullable=True)
    students = db.relationship('Student', backref='classroom', lazy=True)

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        db.session.commit()

class Student(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    roll_no = db.Column(db.String(50), unique=True, nullable=False)
    dept = db.Column(db.String(100), nullable=False)
    phone = db.Column(db.String(15), nullable=True)
    semester = db.Column(db.Integer, default=1) # Sync with classroom.current_semester
    class_id = db.Column(db.Integer, db.ForeignKey('classroom.id'), nullable=True)
    attendance = db.relationship('Attendance', backref='student', lazy=True, cascade="all, delete-orphan")

    def update(self, **kwargs):
        for key, value in kwargs.items():
            if hasattr(self, key):
                setattr(self, key, value)
        db.session.commit()

    def get_attendance_stats(self, subject_id=None):
        query = Attendance.query.filter_by(student_id=self.id)
        if subject_id:
            query = query.filter_by(subject_id=subject_id)
        
        records = query.all()
        total = len(records)
        if total == 0:
            return 0, 0, 0.0
        
        present = len([r for r in records if r.status == 'Present'])
        percentage = (present / total) * 100
        return total, present, round(percentage, 2)

class Subject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    code = db.Column(db.String(20), unique=True, nullable=False)
    dept = db.Column(db.String(100), nullable=True)
    semester = db.Column(db.Integer, default=1) # Mapping only to Semester
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True)
    teacher = db.relationship('User', backref='subjects', lazy=True)
    attendance = db.relationship('Attendance', backref='subject', lazy=True, cascade="all, delete-orphan")

class Attendance(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    student_id = db.Column(db.Integer, db.ForeignKey('student.id'), nullable=False)
    subject_id = db.Column(db.Integer, db.ForeignKey('subject.id'), nullable=False)
    teacher_id = db.Column(db.Integer, db.ForeignKey('user.id'), nullable=True) 
    date = db.Column(db.Date, nullable=False, default=datetime.utcnow)
    status = db.Column(db.String(10), nullable=False) # 'Present' or 'Absent'
