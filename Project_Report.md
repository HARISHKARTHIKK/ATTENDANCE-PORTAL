# Project Report: Student Attendance Management System

## 1. Project Overview
The **Student Attendance Management System** is a web-based application designed to streamline the process of tracking student attendance, managing academic records, and providing real-time analytics for students, teachers, and administrators. Built with a modern tech stack, the system ensures data integrity, ease of use, and multi-role accessibility.

## 2. Objectives
- To automate the traditional manual attendance marking process.
- To provide teachers with an efficient tool for managing classroom attendance.
- To offer students transparency regarding their attendance records and eligibility.
- To enable administrators to manage the core entities: students, teachers, subjects, and departments.
- To implement a penalty system where excessive "Lates" impact the overall attendance percentage.

## 3. Technology Stack
- **Frontend**: HTML5, CSS3 (Vanilla CSS with Responsive Design), JavaScript.
- **Backend Framework**: Flask (Python).
- **Database**: Google Cloud Firestore (NoSQL Document Store).
- **Authentication**: Flask-Login.
- **Data Export**: Pandas and OpenPyXL (Excel Reports).
- **Deployment**: Compatible with platforms like Heroku (Procfile included).

## 4. System Architecture
The application follows a Model-View-Controller (MVC) like architecture adapted for Flask:
- **Models**: Defines the Firestore document structure and business logic (e.g., attendance percentage calculation).
- **Views (Templates)**: Jinja2 templates for rendering the user interface.
- **Controllers (Routes)**: Handles web requests, authentication, and database interactions.

---

## 5. Role-Based Access Control (RBAC)
The system categorizes users into four primary roles:

| Role | Responsibilities |
| :--- | :--- |
| **Admin** | Full system management (Users, Teachers, Students, Classes, Subjects). |
| **Teacher** | Marker for assigned classes, views class reports, exports attendance to Excel. |
| **Security** | Marks students as 'Late' at the entrance (Global Late Marking). |
| **Student** | Views personal attendance dashboard and uses the Attendance Calculator. |

---

## 6. Database Design (Firestore Collections)
The system uses Google Cloud Firestore for flexible, scalable data storage.

### Collections:
1.  **`users`**: Stores credentials and roles for Admins, Teachers, and Security personnel.
2.  **`students`**: Contains student profiles (Name, Roll No, Department, Class, Semester).
3.  **`classrooms`**: Maps classes to specific departments.
4.  **`subjects`**: List of subjects categorized by semester and department.
5.  **`attendance_records`**: Stores individual attendance logs (Date, Student ID, Subject ID, Status).

---

## 7. Key Features

### A. Attendance Tracking
- **Multi-Status Support**: Present, Absent, OD (On Duty), ML (Medical Leave), and Late.
- **Global Late Marking**: Security personnel can mark students late independently of specific subject sessions.

### B. Bulk Student Management
- Administrators can upload student data in bulk using Excel or CSV files, significantly reducing setup time.

### C. Attendance Calculator (For Students)
- A tool for students to predict their future attendance percentage.
- Calculates how many classes they can skip while staying above a required threshold (e.g., 75%).

### D. Automated Penalty Logic
- **The '3 Lates = 1 Leave' Rule**: The system automatically calculates a penalty where three late entries result in the deduction of one present day from the effective attendance.

### E. Reporting & Exports
- Teachers and Admins can filter attendance records by date range, subject, or class.
- One-click **Export to Excel** feature for administrative documentation.

---

## 8. Technical Implementation Details

### Attendance Calculation Logic
The "Effective Attendance" is calculated using the following algorithm:
```python
Penalty = Total_Lates // 3
Effective_Presence = (Present + OD + ML + Session_Lates) - Penalty
Attendance_Percentage = (Effective_Presence / Total_Sessions) * 100
```

### Security Measures
- **Password Hashing**: PBKDF2 with SHA256 for secure credential storage.
- **Session Management**: Secure cookie-based sessions with Flask-Login.
- **Route Protection**: Custom decorators (`@admin_required`, `@teacher_allowed`) ensure horizontal and vertical privilege separation.

---

## 9. Conclusion
The Student Attendance Management System provides a robust and scalable solution for academic institutions. By integrating automated reporting, a mobile-responsive interface, and unique features like global late marking and the attendance calculator, it enhances the efficiency of both administrative tasks and student engagement.
