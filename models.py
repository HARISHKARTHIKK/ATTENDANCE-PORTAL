import firebase_admin
from firebase_admin import credentials, firestore
from flask_login import UserMixin
from datetime import datetime, date
import os

# Firebase is initialized in app.py to ensure environment variables are loaded first
_db_client = None

def get_db():
    global _db_client
    if _db_client is None:
        try:
            _db_client = firestore.client()
        except Exception as e:
            print(f"Error: Firestore client could not be initialized. Ensure Firebase Admin is initialized. {e}")
            raise e
    return _db_client

class FirestoreQuery:
    def __init__(self, model_class):
        self.model_class = model_class
        self.collection_name = getattr(model_class, '__collection__', None)
        self.filters = []
        self._order_by = None
        self._limit = None

    def _get_collection(self):
        if not self.collection_name:
            return None
        return get_db().collection(self.collection_name)

    def filter_by(self, **kwargs):
        for k, v in kwargs.items():
            if v is not None:
                if isinstance(v, list):
                    # Firestore 'in' query
                    self.filters.append((k, 'in', v))
                else:
                    self.filters.append((k, '==', v))
        return self

    def filter(self, *args):
        # Dummy to return self to prevent crashes when SQLAlchemy style .filter() is accidentally called
        return self

    def where(self, field, op, value):
        if value is not None:
            self.filters.append((field, op, value))
        return self

    def order_by(self, *args):
        self._order_by = args
        return self

    def limit(self, val):
        self._limit = val
        return self

    def all(self):
        collection_ref = self._get_collection()
        if not collection_ref: return []
        query = collection_ref
        for f in self.filters:
            query = query.where(f[0], f[1], f[2])
        
        if self._order_by:
            for field in self._order_by:
                fname = field
                if hasattr(field, 'key'): fname = field.key
                if isinstance(fname, str):
                    if fname.startswith('-'):
                        query = query.order_by(fname[1:], direction=firestore.Query.DESCENDING)
                    else:
                        query = query.order_by(fname)
        
        if self._limit:
            query = query.limit(self._limit)
        
        try:
            docs = query.stream()
            results = []
            for doc in docs:
                data = doc.to_dict()
                data['id'] = doc.id
                results.append(self.model_class(**data))
            return results
        except Exception as e:
            print(f"Firestore all() error: {e}")
            return []

    def first(self):
        res = self.limit(1).all()
        return res[0] if res else None

    def count(self):
        # For simplicity in this migration
        return len(self.all())

    def get(self, doc_id):
        collection_ref = self._get_collection()
        if not doc_id or not collection_ref: return None
        try:
            doc = collection_ref.document(str(doc_id)).get()
            if doc.exists:
                data = doc.to_dict()
                data['id'] = doc.id
                return self.model_class(**data)
        except Exception as e:
            print(f"Firestore get() error: {e}")
        return None

    def get_or_404(self, doc_id):
        from flask import abort
        res = self.get(doc_id)
        if not res:
            abort(404)
        return res

    def join(self, *args): return self

class FirestoreSession:
    def add(self, obj):
        if hasattr(obj, 'save'):
            obj.save()
    def commit(self): pass
    def delete(self, obj):
        if hasattr(obj, 'id') and obj.id:
            get_db().collection(obj.__collection__).document(str(obj.id)).delete()
    def rollback(self): pass
    def flush(self): pass

class ModelMeta(type):
    @property
    def query(cls):
        return FirestoreQuery(cls)

class FirestoreModel(metaclass=ModelMeta):
    __collection__ = None
    def __init__(self, **kwargs):
        self.id = kwargs.pop('id', None)
        for k, v in kwargs.items():
            setattr(self, k, v)

    def save(self):
        data = self.__dict__.copy()
        doc_id = data.pop('id', None)
        clean_data = {}
        for k, v in data.items():
            if k.startswith('_') or callable(v): continue
            if isinstance(v, (date, datetime)):
                if isinstance(v, date) and not isinstance(v, datetime):
                    v = datetime.combine(v, datetime.min.time())
            clean_data[k] = v

        if doc_id:
            get_db().collection(self.__collection__).document(str(doc_id)).set(clean_data)
        else:
            _, doc_ref = get_db().collection(self.__collection__).add(clean_data)
            self.id = doc_ref.id

    def update(self, **kwargs):
        for k, v in kwargs.items():
            setattr(self, k, v)
        self.save()

    def delete(self):
        if self.id:
            get_db().collection(self.__collection__).document(str(self.id)).delete()

class DBWrapper:
    def __init__(self):
        self.session = FirestoreSession()
        self.Model = FirestoreModel
    def init_app(self, app): pass
    def create_all(self): pass

db = DBWrapper()

class User(UserMixin, FirestoreModel):
    __collection__ = 'users'
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if not hasattr(self, 'role'): self.role = 'teacher'
        if not hasattr(self, 'assigned_classes_ids'): self.assigned_classes_ids = []

    def get_id(self): return str(self.id)

    @property
    def assigned_classes(self):
        if not hasattr(self, 'assigned_classes_ids') or not self.assigned_classes_ids:
            return FirestoreQuery(Classroom).filter_by(id='NONE')
        return FirestoreQuery(Classroom).filter_by(id=self.assigned_classes_ids)

    @assigned_classes.setter
    def assigned_classes(self, value):
        if isinstance(value, list) and len(value) == 0:
            self.assigned_classes_ids = []
            # Return a special list that intercepts .append(cls)
            # Python doesn't allow returning from setter, but we can set an attribute
            self._mock_list = MockRelationList(self, 'assigned_classes_ids')

    def __getattribute__(self, name):
        if name == 'assigned_classes' and hasattr(self, '_mock_list'):
            # If we are in the middle of an assignment (after = [])
            # we want to return the mock list so .append() works
            return self._mock_list
        return super().__getattribute__(name)

class MockRelationList(list):
    def __init__(self, owner, attr_name):
        self.owner = owner
        self.attr_name = attr_name
        super().__init__()
    def append(self, item):
        if not hasattr(self.owner, self.attr_name) or getattr(self.owner, self.attr_name) is None:
            setattr(self.owner, self.attr_name, [])
        if hasattr(item, 'id'):
            getattr(self.owner, self.attr_name).append(str(item.id))
        super().append(item)



class Department(FirestoreModel):
    __collection__ = 'departments'

class Classroom(FirestoreModel):
    __collection__ = 'classrooms'

class Student(FirestoreModel):
    __collection__ = 'students'
    def get_attendance_stats(self, subject_id=None):
        query = Attendance.query.filter_by(student_id=self.id)
        if subject_id:
            query = query.filter_by(subject_id=subject_id)
        records = query.all()
        total = len(records)
        if total == 0: return 0, 0, 0.0
        present = len([r for r in records if getattr(r, 'status', '') == 'Present'])
        percentage = (present / total) * 100
        return total, present, round(percentage, 2)

class Subject(FirestoreModel):
    __collection__ = 'subjects'

class Attendance(FirestoreModel):
    __collection__ = 'attendance_records'
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        if hasattr(self, 'date') and isinstance(self.date, date) and not isinstance(self.date, datetime):
            self.date = datetime.combine(self.date, datetime.min.time())
