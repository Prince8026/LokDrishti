import os
from datetime import datetime
from flask import Flask, render_template, request, redirect, url_for, send_from_directory
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, UserMixin, login_user, login_required, logout_user, current_user
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask_migrate import Migrate

app = Flask(__name__)
app.secret_key = "secretkey"
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///users.db'
app.config['UPLOAD_FOLDER'] = 'uploads'
os.makedirs(app.config['UPLOAD_FOLDER'], exist_ok=True)

db = SQLAlchemy(app)
migrate = Migrate(app, db)

login_manager = LoginManager(app)
login_manager.login_view = 'login'


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(150), unique=True, nullable=False)
    password = db.Column(db.String(255), nullable=False)


class Inspection(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey('user.id'))
    photo_path = db.Column(db.String(200))
    problem = db.Column(db.String(500))
    category = db.Column(db.String(100))
    other_category = db.Column(db.String(200))
    latitude = db.Column(db.String(50))
    longitude = db.Column(db.String(50))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    status = db.Column(db.String(20), default="Not Seen")
    resolved_date = db.Column(db.String(50), default="-")
    reply = db.Column(db.String(500), default="")
    priority = db.Column(db.String(50))
    rating = db.Column(db.Integer, nullable=True)
    rating_comment = db.Column(db.Text, nullable=True)
    # Bumped automatically on every insert/update — lets the frontend detect
    # "something changed" without comparing every field itself.
    last_updated = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    user = db.relationship('User', backref=db.backref('inspections', lazy=True))


CATEGORIES = [
    "Potholes and street damage",
    "Missed garbage collection",
    "Illegal dumping and littering",
    "Streetlight outages",
    "Parking violations and blocked driveways",
    "Other",
]

PRIORITIES = [
    "Urgent & Important",
    "Important but Not Urgent",
    "Urgent but Not Important",
    "Low Urgency & Low Impact",
]

# Lower number = higher priority, used for the "Sort by Priority" option.
PRIORITY_RANK = {name: rank for rank, name in enumerate(PRIORITIES, start=1)}


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


def is_admin():
    return current_user.is_authenticated and current_user.username == "prince"


def make_admin():
    """Create the fixed admin account or reset its password to the requested value."""
    admin = User.query.filter_by(username="prince").first()

    if admin is None:
        admin = User(
            username="prince",
            password=generate_password_hash("2610")
        )
        db.session.add(admin)
    else:
        if not check_password_hash(admin.password, "2610"):
            admin.password = generate_password_hash("2610")

    db.session.commit()


def format_date(date_text):
    """Convert HTML date value YYYY-MM-DD to DD-MM-YYYY."""
    if not date_text:
        return "-"
    try:
        return datetime.strptime(date_text, "%Y-%m-%d").strftime("%d-%m-%Y")
    except ValueError:
        return "-"


def parse_resolve_date(inspection):
    try:
        return datetime.strptime(inspection.resolved_date, "%d-%m-%Y")
    except (TypeError, ValueError):
        return None


def priority_rank(inspection):
    return PRIORITY_RANK.get(inspection.priority)


def compute_version(inspections):
    """Cheap signature for a set of inspections: changes whenever a row is
    added/removed, or any field on any row is edited. Used by the frontend
    to poll for changes without re-fetching and diffing the full dataset.
    """
    if not inspections:
        return "empty"
    latest = max((i.last_updated or i.timestamp) for i in inspections)
    return f"{len(inspections)}:{latest.isoformat()}"


def get_filtered_sorted_inspections():
    """Shared filter/sort logic used by both the admin dashboard and the report page.

    Reads 'status', 'sort', and 'order' from the query string, and returns
    (inspections, status_filter, sort_key, order) with the query-string
    values normalized to what was actually applied.
    """
    status_filter = request.args.get('status', '').strip()
    sort_key = request.args.get('sort', 'id').strip()
    order = request.args.get('order', 'asc').strip()

    if sort_key not in ('id', 'resolve_date', 'priority'):
        sort_key = 'id'
    if order not in ('asc', 'desc'):
        order = 'asc'

    reverse = (order == 'desc')

    if status_filter in ["Not Seen", "In Progress", "Completed"]:
        inspections = Inspection.query.filter_by(status=status_filter).all()
    else:
        inspections = Inspection.query.all()

    if sort_key == 'resolve_date':
        dated = [i for i in inspections if parse_resolve_date(i) is not None]
        undated = [i for i in inspections if parse_resolve_date(i) is None]

        # Secondary tie-break: within the same resolve date, the higher-priority
        # problem (1 = Urgent & Important) comes first. Problems with no priority
        # set yet fall to the end of their date's tie group either way.
        dated_ranked = [i for i in dated if priority_rank(i) is not None]
        dated_unranked = [i for i in dated if priority_rank(i) is None]
        dated_ranked.sort(key=priority_rank)
        dated = dated_ranked + dated_unranked

        # Primary sort: resolve date, respecting the chosen order. Sort is
        # stable, so items sharing a date keep the priority order set above.
        dated.sort(key=parse_resolve_date, reverse=reverse)

        # Problems with no resolve date yet always sit at the end, either way.
        inspections = dated + undated

    elif sort_key == 'priority':
        ranked = [i for i in inspections if priority_rank(i) is not None]
        unranked = [i for i in inspections if priority_rank(i) is None]

        # Secondary tie-break: within the same priority, the soonest resolve
        # date comes first. Problems with no resolve date yet fall to the end
        # of their priority's tie group either way.
        ranked_dated = [i for i in ranked if parse_resolve_date(i) is not None]
        ranked_undated = [i for i in ranked if parse_resolve_date(i) is None]
        ranked_dated.sort(key=parse_resolve_date)
        ranked = ranked_dated + ranked_undated

        # Primary sort: priority rank (1 = Urgent & Important highest,
        # 4 = Low Urgency & Low Impact lowest), respecting the chosen order.
        # Sort is stable, so items sharing a priority keep the date order set above.
        ranked.sort(key=priority_rank, reverse=reverse)

        # Problems with no priority set yet always sit at the end, either way.
        inspections = ranked + unranked

    else:
        inspections.sort(key=lambda i: i.id, reverse=reverse)

    return inspections, status_filter, sort_key, order


def build_report_analytics():
    """Aggregate stats across ALL problems (unfiltered) for the report's charts."""
    all_inspections = Inspection.query.all()

    status_labels = ["Not Seen", "In Progress", "Completed"]
    status_counts = [
        sum(1 for i in all_inspections if i.status == label)
        for label in status_labels
    ]

    category_counts = [
        sum(1 for i in all_inspections if i.category == label)
        for label in CATEGORIES
    ]

    priority_labels = PRIORITIES + ["Unassigned"]
    priority_counts = [
        sum(1 for i in all_inspections if i.priority == label)
        for label in PRIORITIES
    ]
    priority_counts.append(
        sum(1 for i in all_inspections if i.priority not in PRIORITY_RANK)
    )

    rated = [i for i in all_inspections if i.rating]
    rating_counts = [sum(1 for i in rated if i.rating == star) for star in range(1, 6)]
    avg_rating = round(sum(i.rating for i in rated) / len(rated), 2) if rated else None

    total = len(all_inspections)
    completed = sum(1 for i in all_inspections if i.status == "Completed")

    return {
        "total": total,
        "completed": completed,
        "completion_rate": round((completed / total) * 100, 1) if total else 0,
        "status_labels": status_labels,
        "status_counts": status_counts,
        "category_labels": CATEGORIES,
        "category_counts": category_counts,
        "priority_labels": priority_labels,
        "priority_counts": priority_counts,
        "rating_counts": rating_counts,
        "rated_count": len(rated),
        "avg_rating": avg_rating,
    }


@app.route('/register', methods=['GET', 'POST'])
def register():
    error = None

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        # The fixed admin account cannot be created through public registration.
        if username.lower() == "prince":
            error = "The username 'prince' is reserved for the administrator."
        elif not username or not password:
            error = "Username and password are required."
        elif User.query.filter_by(username=username).first():
            error = "Username already exists. Please choose another username."
        else:
            hashed_password = generate_password_hash(password)
            new_user = User(username=username, password=hashed_password)
            db.session.add(new_user)
            db.session.commit()
            return redirect(url_for('login'))

    return render_template('register.html', error=error)


@app.route('/login', methods=['GET', 'POST'])
def login():
    error = None

    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        password = request.form.get('password', '')

        user = User.query.filter_by(username=username).first()

        if user and check_password_hash(user.password, password):
            login_user(user)
            return redirect(url_for('dashboard'))

        error = "Invalid username or password."

    return render_template('login.html', error=error)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    return redirect(url_for('login'))


@app.route('/dashboard')
@login_required
def dashboard():
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    # Normal registered users can see ONLY their own submitted problems.
    inspections = Inspection.query.filter_by(user_id=current_user.id).order_by(
        Inspection.timestamp.desc()
    ).all()

    return render_template('dashboard.html', inspections=inspections, version=compute_version(inspections))


@app.route('/api/dashboard_version')
@login_required
def dashboard_version():
    # Lightweight endpoint the dashboard polls to know whether it needs to
    # refresh — scoped to just this user's own problems.
    if is_admin():
        return {"version": "n/a"}

    inspections = Inspection.query.filter_by(user_id=current_user.id).all()
    return {"version": compute_version(inspections)}


@app.route('/upload', methods=['GET', 'POST'])
@login_required
def upload():
    # The administrator does not use the public upload page.
    if is_admin():
        return redirect(url_for('admin_dashboard'))

    if request.method == 'POST':
        file = request.files.get('photo')
        lat = request.form.get('lat')
        lon = request.form.get('lon')
        problem = request.form.get('problem', '').strip()
        category = request.form.get('category', '').strip()
        other_category = request.form.get('other_category', '').strip()

        if file and file.filename and problem and category in CATEGORIES:
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))

            new_inspection = Inspection(
                user_id=current_user.id,
                photo_path=filename,
                problem=problem,
                category=category,
                other_category=other_category if category == "Other" else "",
                latitude=lat,
                longitude=lon,
                status="Not Seen",
                resolved_date="-",
                reply=""
            )

            db.session.add(new_inspection)
            db.session.commit()
            return redirect(url_for('dashboard'))

    return render_template('upload.html')


@app.route('/uploads/<filename>')
@login_required
def uploaded_file(filename):
    return send_from_directory(app.config['UPLOAD_FOLDER'], filename)


@app.route('/rate/<int:inspection_id>', methods=['POST'])
@login_required
def rate_inspection(inspection_id):
    inspection = db.session.get(Inspection, inspection_id)

    if inspection is None:
        return "Problem not found", 404

    # Users can only rate their own completed problems.
    if inspection.user_id != current_user.id or inspection.status != "Completed":
        return "Access denied", 403

    try:
        rating_value = int(request.form.get('rating', ''))
    except ValueError:
        rating_value = None

    if rating_value is not None and 1 <= rating_value <= 5:
        inspection.rating = rating_value
        inspection.rating_comment = request.form.get('rating_comment', '').strip()
        db.session.commit()

    return redirect(url_for('dashboard'))


@app.route('/admin_dashboard', methods=['GET', 'POST'])
@login_required
def admin_dashboard():
    if not is_admin():
        return "Access denied", 403

    if request.method == 'POST':
        inspection_id = request.form.get('inspection_id')
        inspection = db.session.get(Inspection, int(inspection_id))

        if inspection is None:
            return "Problem not found", 404

        new_status = request.form.get('status', 'Not Seen')
        new_resolve_date = request.form.get('resolved_date', '')
        admin_reply = request.form.get('reply', '').strip()
        new_priority = request.form.get('priority')

        # Only the three requested statuses are accepted.
        if new_status not in ["Not Seen", "In Progress", "Completed"]:
            new_status = "Not Seen"

        if new_priority in PRIORITIES:
            inspection.priority = new_priority

        # If an admin selects a resolve date, the problem becomes In Progress
        # unless the admin explicitly marks it Completed.
        if new_resolve_date:
            inspection.resolved_date = format_date(new_resolve_date)
            if new_status == "Not Seen":
                new_status = "In Progress"
        else:
            if new_status == "Not Seen":
                inspection.resolved_date = "-"
            else:
                # In Progress / Completed should have a resolve date.
                inspection.resolved_date = inspection.resolved_date if inspection.resolved_date != "-" else "-"

        inspection.status = new_status
        inspection.reply = admin_reply

        db.session.commit()
        return redirect(url_for('admin_dashboard'))

    inspections, status_filter, sort_key, order = get_filtered_sorted_inspections()

    return render_template(
        'admin_dashboard.html',
        inspections=inspections,
        selected_status=status_filter,
        selected_sort=sort_key,
        selected_order=order,
        version=compute_version(Inspection.query.all())
    )


@app.route('/api/admin_dashboard_version')
@login_required
def admin_dashboard_version():
    # Scoped to ALL problems (not just the currently filtered view) so any
    # change anywhere is caught, regardless of which status filter is active.
    if not is_admin():
        return "Access denied", 403

    return {"version": compute_version(Inspection.query.all())}


@app.route('/admin_report')
@login_required
def admin_report():
    # Read-only analytics page, reachable only via the admin dashboard's
    # "Report" button, and only ever accessible to the admin account.
    if not is_admin():
        return "Access denied", 403

    inspections, status_filter, sort_key, order = get_filtered_sorted_inspections()
    analytics = build_report_analytics()

    return render_template(
        'report.html',
        inspections=inspections,
        selected_status=status_filter,
        selected_sort=sort_key,
        selected_order=order,
        analytics=analytics,
        version=compute_version(Inspection.query.all())
    )


if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        make_admin()

    app.run(debug=True, use_reloader=False, port=8000)