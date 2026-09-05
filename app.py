from flask import Flask, render_template, request, redirect, url_for, session, jsonify
from flask_mysqldb import MySQL
from werkzeug.security import generate_password_hash, check_password_hash
from werkzeug.utils import secure_filename
from datetime import date, datetime
import os
import random
import json
import string
import re

app = Flask(__name__)
app.secret_key = os.environ.get('SECRET_KEY', 'campuscycle_secret_key_bracu_2026')

UPLOAD_FOLDER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads')
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
app.config['UPLOAD_FOLDER'] = UPLOAD_FOLDER
app.config['MAX_CONTENT_LENGTH'] = 16 * 1024 * 1024  # 16 MB

app.config['MYSQL_HOST'] = 'localhost'
app.config['MYSQL_USER'] = 'root'
app.config['MYSQL_PASSWORD'] = ''
app.config['MYSQL_DB'] = 'campuscycle'
app.config['MYSQL_CURSORCLASS'] = 'DictCursor'

mysql = MySQL(app)


def ensure_db_schema():
    """Ensures custom wishlist tables and necessary schema extensions exist."""
    try:
        cur = mysql.connection.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS wishlist_custom_item (
                item_id INT AUTO_INCREMENT PRIMARY KEY,
                wishlist_id INT NOT NULL,
                student_id VARCHAR(50) NOT NULL,
                item_name VARCHAR(150) NOT NULL,
                category VARCHAR(50),
                target_price DECIMAL(10,2) NULL,
                used_in_course VARCHAR(100) NULL,
                notes TEXT NULL,
                status VARCHAR(20) DEFAULT 'looking',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                INDEX (student_id),
                INDEX (wishlist_id)
            )
        """)
        mysql.connection.commit()
        cur.close()
    except Exception:
        pass


def create_digital_receipt(order_id, buyer_id, buyer_name, amount, payment_method, account_number, trx_id, delivery_place, items):
    """Generates a structured JSON digital receipt for an order."""
    if not trx_id:
        prefix_map = {
            'bkash': 'BK',
            'nagad': 'NG',
            'rocket': 'RK',
            'card': 'CD',
            'cash_on_meetup': 'CSH'
        }
        prefix = prefix_map.get(payment_method, 'TX')
        random_suffix = ''.join(random.choices(string.ascii_uppercase + string.digits, k=8))
        trx_id = f"{prefix}{random_suffix}"

    method_labels = {
        'bkash': 'bKash Mobile Payment',
        'nagad': 'Nagad Digital Wallet',
        'rocket': 'Dutch-Bangla Rocket',
        'card': 'Credit / Debit Card (Online)',
        'cash_on_meetup': 'Cash on Campus Handover'
    }

    masked_acc = account_number
    if account_number and len(account_number) >= 8:
        masked_acc = account_number[:3] + '****' + account_number[-4:]
    elif payment_method == 'card' and account_number:
        masked_acc = '**** **** **** ' + account_number[-4:]
    elif not account_number:
        masked_acc = 'N/A (Campus Handover)' if payment_method == 'cash_on_meetup' else 'N/A'

    is_paid = payment_method in ['bkash', 'nagad', 'rocket', 'card']

    receipt_data = {
        'receipt_no': f"CC-REC-{order_id:05d}",
        'order_id': order_id,
        'trx_id': trx_id,
        'payment_method': method_labels.get(payment_method, payment_method.capitalize()),
        'payment_method_code': payment_method,
        'account_number': masked_acc,
        'amount': float(amount or 0),
        'payment_status': 'PAID (Verified)' if is_paid else 'PENDING CASH ON HANDOVER',
        'is_paid': is_paid,
        'issued_at': datetime.now().strftime('%d %b %Y, %I:%M %p'),
        'buyer_id': buyer_id,
        'buyer_name': buyer_name,
        'delivery_place': delivery_place or 'UB Gate / Building Lobby',
        'items': items or []
    }
    return json.dumps(receipt_data)


def compute_product_age(purchase_date):
    """Derives human-readable item age from original purchase date."""
    if not purchase_date:
        return "Not specified"
    if isinstance(purchase_date, str):
        try:
            purchase_date = datetime.strptime(purchase_date, '%Y-%m-%d').date()
        except Exception:
            return "Recently"
    today = date.today()
    days = (today - purchase_date).days
    if days < 0:
        return "Brand New"
    if days < 30:
        return f"{days} days old"
    months = days // 30
    if months < 12:
        return f"{months} month{'s' if months > 1 else ''} old"
    years = round(days / 365, 1)
    return f"{years} year{'s' if years != 1 else ''} old"


def extract_course_prefixes(course_str):
    """
    Extracts 3-letter uppercase departmental course prefixes from a course string.
    Handles single courses ('CSE110', 'CSE 110') and lists ('CSE260, CSE250; EEE241').
    Returns a set of 3-letter uppercase prefixes, e.g. {'CSE', 'EEE'}.
    """
    if not course_str:
        return set()
    prefixes = set()
    tokens = re.split(r'[,;/&+]|\band\b', str(course_str), flags=re.IGNORECASE)
    for token in tokens:
        letters = re.sub(r'[^a-zA-Z]', '', token).upper()
        if len(letters) >= 3:
            prefixes.add(letters[:3])
    return prefixes


def compute_recommended_price(selling_price, category=None):
    """Calculates Derived Attribute: Recommended Price based on category & fair market discount."""
    try:
        price = float(selling_price)
    except (ValueError, TypeError):
        return 0.0
    factor = 0.95
    if category == 'Books':
        factor = 0.92
    elif category == 'Electronics':
        factor = 0.94
    elif category == 'Scientific Calculator':
        factor = 0.90
    return round(price * factor, 2)


def get_cart_count(student_id):
    """Returns the total number of products added to student's cart."""
    if not student_id:
        return 0
    try:
        cur = mysql.connection.cursor()
        cur.execute("""
            SELECT COUNT(a.product_id) as count
            FROM cart c
            JOIN added a ON c.cart_id = a.cart_id
            WHERE c.student_id = %s
        """, (student_id,))
        row = cur.fetchone()
        cur.close()
        return int(row['count']) if row and row['count'] is not None else 0
    except Exception:
        return 0


def get_wishlist_count(student_id):
    """Returns the total number of products and custom requests saved in student's wishlist."""
    if not student_id:
        return 0
    try:
        ensure_db_schema()
        cur = mysql.connection.cursor()
        cur.execute("""
            SELECT COUNT(i.product_id) as count
            FROM wishlist w
            JOIN includes i ON w.wishlist_id = i.wishlist_id
            WHERE w.student_id = %s
        """, (student_id,))
        row = cur.fetchone()
        count = int(row['count']) if row and row['count'] is not None else 0

        try:
            cur.execute("""
                SELECT COUNT(item_id) as count
                FROM wishlist_custom_item
                WHERE student_id = %s
            """, (student_id,))
            c_row = cur.fetchone()
            if c_row and c_row['count'] is not None:
                count += int(c_row['count'])
        except Exception:
            pass

        cur.close()
        return count
    except Exception:
        return 0


def get_unread_notifications_count(student_id):
    """Returns the count of unread notifications for a student."""
    if not student_id:
        return 0
    try:
        cur = mysql.connection.cursor()
        cur.execute("""
            SELECT COUNT(*) as count
            FROM notification
            WHERE buyer_notification = %s AND notification_status = 'unread'
        """, (student_id,))
        row = cur.fetchone()
        cur.close()
        return int(row['count']) if row and row['count'] is not None else 0
    except Exception:
        return 0


@app.context_processor
def inject_global_counts():
    """Injects cart, wishlist, and unread notification counts globally across all templates."""
    student_id = session.get('student_id')
    if student_id:
        return {
            'global_cart_count': get_cart_count(student_id),
            'global_wishlist_count': get_wishlist_count(student_id),
            'global_unread_notifications_count': get_unread_notifications_count(student_id)
        }
    return {
        'global_cart_count': 0,
        'global_wishlist_count': 0,
        'global_unread_notifications_count': 0
    }


def format_message_time(dt):
    """Formats timestamp into a friendly human-readable format."""
    if not dt:
        return ""
    if isinstance(dt, str):
        try:
            dt = datetime.strptime(dt, '%Y-%m-%d %H:%M:%S')
        except Exception:
            return dt
    now = datetime.now()
    diff = now - dt
    if diff.days == 0:
        return dt.strftime('%I:%M %p')
    elif diff.days == 1:
        return 'Yesterday, ' + dt.strftime('%I:%M %p')
    elif diff.days < 7:
        return dt.strftime('%a, %I:%M %p')
    else:
        return dt.strftime('%b %d, %I:%M %p')


@app.route('/')
def home():
    student_id = session.get('student_id')
    cart_count = get_cart_count(student_id) if student_id else 0
    wishlist_count = get_wishlist_count(student_id) if student_id else 0
    return render_template('home.html', cart_count=cart_count, wishlist_count=wishlist_count)


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if session.get('student_id'):
        return redirect(url_for('profile'))

    if request.method == 'POST':
        student_id = request.form.get('student_id', '').strip()
        name = request.form.get('name', '').strip()
        email = request.form.get('gsuite_email', '').strip()
        password = request.form.get('password', '')
        mobile = request.form.get('mobile_number', '').strip()
        department = request.form.get('department', '').strip()
        semester = request.form.get('semester', '').strip()

        if not (email.endswith('@g.bracu.ac.bd') or email.endswith('@bracu.ac.bd')):
            return render_template(
                'signup.html', 
                error="Only BRACU email (@g.bracu.ac.bd or @bracu.ac.bd) is allowed."
            )

        cur = mysql.connection.cursor()
        cur.execute(
            "SELECT * FROM user WHERE gsuite_email = %s OR student_id = %s",
            (email, student_id)
        )
        existing_user = cur.fetchone()

        if existing_user:
            cur.close()
            return render_template(
                'signup.html', 
                error="An account with this Email or Student ID already exists. Please login."
            )

        password_hash = generate_password_hash(password)

        cur.execute("""
            INSERT INTO user
            (student_id, name, gsuite_email, mobile_number,
             department, semester, password_hash, trust_score)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """, (
            student_id,
            name,
            email,
            mobile,
            department,
            semester if semester else None,
            password_hash,
            5.0
        ))

        # Create verification record
        cur.execute("""
            INSERT INTO verification (verification_status, verified_at, student_id)
            VALUES ('verified', NOW(), %s)
        """, (student_id,))

        # Initialize student cart
        cur.execute("INSERT INTO cart (student_id, total_bill) VALUES (%s, 0)", (student_id,))

        mysql.connection.commit()
        cur.close()

        session['student_id'] = student_id
        session['user_name'] = name
        session['gsuite_email'] = email

        return redirect(url_for('profile'))

    return render_template('signup.html')


@app.route('/login', methods=['GET', 'POST'])
def login():
    if session.get('student_id'):
        return redirect(url_for('profile'))

    if request.method == 'POST':
        identifier = request.form.get('identifier', '').strip()
        password = request.form.get('password', '')

        if not identifier or not password:
            return render_template('login.html', error="Please provide both Email/Student ID and Password.", identifier=identifier)

        cur = mysql.connection.cursor()
        cur.execute(
            "SELECT * FROM user WHERE gsuite_email = %s OR student_id = %s",
            (identifier, identifier)
        )
        user = cur.fetchone()

        if not user:
            cur.close()
            return render_template('login.html', error="No account found with this Email or Student ID. Please sign up.", identifier=identifier)

        password_valid = False
        stored_hash = user.get('password_hash')

        if stored_hash:
            if stored_hash.startswith(('pbkdf2:', 'scrypt:', 'argon2:')):
                password_valid = check_password_hash(stored_hash, password)
            else:
                password_valid = (stored_hash == password)
                if password_valid:
                    new_hash = generate_password_hash(password)
                    cur.execute("UPDATE user SET password_hash = %s WHERE student_id = %s", (new_hash, user['student_id']))
                    mysql.connection.commit()
        else:
            password_valid = True
            new_hash = generate_password_hash(password)
            cur.execute("UPDATE user SET password_hash = %s WHERE student_id = %s", (new_hash, user['student_id']))
            mysql.connection.commit()

        cur.close()

        if not password_valid:
            return render_template('login.html', error="Invalid password. Please check your credentials.", identifier=identifier)

        session['student_id'] = user['student_id']
        session['user_name'] = user['name']
        session['gsuite_email'] = user['gsuite_email']

        return redirect(url_for('profile'))

    return render_template('login.html')


@app.route('/profile')
@app.route('/profile/<student_id>')
def profile(student_id=None):
    current_student_id = session.get('student_id')
    if not current_student_id:
        return redirect(url_for('login'))

    target_student_id = student_id if student_id else current_student_id
    is_own_profile = (target_student_id == current_student_id)

    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM user WHERE student_id = %s", (target_student_id,))
    user = cur.fetchone()

    if not user:
        cur.close()
        return redirect(url_for('profile'))

    cur.execute(
        "SELECT * FROM verification WHERE student_id = %s ORDER BY verification_id DESC LIMIT 1", 
        (target_student_id,)
    )
    verification = cur.fetchone()

    if not verification:
        cur.execute(
            "INSERT INTO verification (verification_status, verified_at, student_id) VALUES ('verified', NOW(), %s)",
            (target_student_id,)
        )
        mysql.connection.commit()
        cur.execute(
            "SELECT * FROM verification WHERE student_id = %s ORDER BY verification_id DESC LIMIT 1", 
            (target_student_id,)
        )
        verification = cur.fetchone()

    cur.execute(
        "SELECT AVG(rating) as avg_rating, COUNT(r_id) as total_reviews FROM review WHERE reviewee_id = %s",
        (target_student_id,)
    )
    trust_data = cur.fetchone()

    if trust_data and trust_data['total_reviews'] and trust_data['total_reviews'] > 0:
        derived_trust_score = float(trust_data['avg_rating'])
        total_reviews = trust_data['total_reviews']
    elif user.get('trust_score') is not None and float(user['trust_score']) > 0:
        derived_trust_score = float(user['trust_score'])
        total_reviews = 1
    else:
        derived_trust_score = 5.0
        total_reviews = 0

    cur.execute("""
        SELECT o.order_id, o.final_bill, o.payment_method, o.delivery_date, 
               o.delivery_place, o.delivery_status, o.order_type, o.receipt, o.confirmation,
               p.product_id, p.product_name, p.category, p.selling_price, p.student_id as seller_id, u.name as seller_name,
               r.rating as buyer_rating, r.comment as buyer_comment,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
        FROM orders o
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN user u ON p.student_id = u.student_id
        LEFT JOIN review r ON r.order_id = o.order_id AND r.reviewer_id = o.st_buyer_id
        WHERE o.st_buyer_id = %s AND (o.order_type = 'buy' OR o.order_type IS NULL)
        ORDER BY o.order_id DESC
    """, (target_student_id,))
    raw_purchases = cur.fetchall() or []
    purchases = []
    for pur in raw_purchases:
        pur_dict = dict(pur)
        if pur_dict.get('receipt'):
            try:
                pur_dict['receipt_data'] = json.loads(pur_dict['receipt'])
            except Exception:
                pur_dict['receipt_data'] = None
        else:
            pur_dict['receipt_data'] = None
        purchases.append(pur_dict)

    cur.execute("""
        SELECT product_id, product_name, category, description,
               selling_price, recommended_price, warranty, used_in_course,
               purchase_date, sold_date, order_id,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = product.product_id LIMIT 1) as photo
        FROM product
        WHERE student_id = %s
        ORDER BY product_id DESC
    """, (target_student_id,))
    sales = cur.fetchall() or []

    cur.execute("""
        SELECT o.order_id, o.final_bill, o.delivery_date, 
               o.delivery_place, o.delivery_status, o.order_type,
               p.product_name, p.category
        FROM orders o
        LEFT JOIN cart c ON o.cart_id = c.cart_id
        LEFT JOIN added a ON c.cart_id = a.cart_id
        LEFT JOIN product p ON a.product_id = p.product_id
        WHERE (o.st_buyer_id = %s OR p.student_id = %s) AND o.order_type = 'exchange'
        ORDER BY o.order_id DESC
    """, (target_student_id, target_student_id))
    exchanges = cur.fetchall() or []

    # Fetch verified reviews received by this student
    cur.execute("""
        SELECT r.r_id, r.rating, r.comment, r.review_date, r.order_id,
               u.name as reviewer_name, u.student_id as reviewer_student_id,
               p.product_name
        FROM review r
        JOIN user u ON r.reviewer_id = u.student_id
        LEFT JOIN orders o ON r.order_id = o.order_id
        LEFT JOIN product p ON p.order_id = o.order_id
        WHERE r.reviewee_id = %s
        ORDER BY r.review_date DESC
    """, (target_student_id,))
    reviews_received = cur.fetchall() or []

    cur.close()
    cart_count = get_cart_count(current_student_id)
    wishlist_count = get_wishlist_count(current_student_id)

    return render_template(
        'profile.html',
        user=user,
        verification=verification,
        derived_trust_score=derived_trust_score,
        total_reviews=total_reviews,
        purchases=purchases,
        sales=sales,
        exchanges=exchanges,
        reviews_received=reviews_received,
        cart_count=cart_count,
        wishlist_count=wishlist_count,
        is_own_profile=is_own_profile
    )



@app.route('/update-profile', methods=['POST'])
def update_profile():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    department = request.form.get('department')
    semester = request.form.get('semester')
    mobile_number = request.form.get('mobile_number')

    cur = mysql.connection.cursor()
    cur.execute("""
        UPDATE user 
        SET department = %s, semester = %s, mobile_number = %s
        WHERE student_id = %s
    """, (department, semester, mobile_number, student_id))
    mysql.connection.commit()
    cur.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({
            'success': True,
            'department': department,
            'semester': semester,
            'mobile_number': mobile_number,
            'message': 'Academic profile updated successfully!'
        })

    return redirect(url_for('profile'))



@app.route('/marketplace')
def marketplace():
    student_id = session.get('student_id')
    cur = mysql.connection.cursor()

    # Active / Available items
    cur.execute("""
        SELECT p.product_id, p.product_name, p.category, p.description,
               p.selling_price, p.recommended_price, p.used_in_course,
               p.purchase_date, p.sold_date, p.order_id, p.student_id AS seller_id,
               u.name AS seller_name,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
        FROM product p
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE p.sold_date IS NULL AND p.order_id IS NULL
        ORDER BY p.product_id DESC
    """)
    available_products = list(cur.fetchall() or [])

    # Recently Sold items
    cur.execute("""
        SELECT p.product_id, p.product_name, p.category, p.description,
               p.selling_price, p.recommended_price, p.used_in_course,
               p.purchase_date, p.sold_date, p.order_id, p.student_id AS seller_id,
               u.name AS seller_name,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
        FROM product p
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE p.sold_date IS NOT NULL OR p.order_id IS NOT NULL
        ORDER BY COALESCE(p.sold_date, CURDATE()) DESC, p.product_id DESC
    """)
    sold_products = list(cur.fetchall() or [])

    # Product-specific review ratings map
    cur.execute("""
        SELECT COALESCE(p.product_id, JSON_UNQUOTE(JSON_EXTRACT(o.receipt, '$.items[0].product_id')), a.product_id) as pid,
               AVG(r.rating) as avg_rating, COUNT(DISTINCT r.r_id) as review_count
        FROM review r
        JOIN orders o ON r.order_id = o.order_id
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN added a ON a.cart_id = o.cart_id
        GROUP BY pid
    """)
    review_stat_rows = cur.fetchall() or []
    review_stat_map = {}
    for rs in review_stat_rows:
        if rs.get('pid'):
            try:
                pid = int(rs['pid'])
                review_stat_map[pid] = {
                    'avg_rating': round(float(rs['avg_rating']), 1),
                    'review_count': int(rs['review_count'])
                }
            except Exception:
                pass

    for p in available_products:
        p['age'] = compute_product_age(p.get('purchase_date'))
        p_stats = review_stat_map.get(p['product_id'], {'avg_rating': 0.0, 'review_count': 0})
        p['avg_rating'] = p_stats['avg_rating']
        p['review_count'] = p_stats['review_count']

    for p in sold_products:
        if not p.get('sold_date'):
            p['sold_date'] = date.today().strftime('%Y-%m-%d')
        p['age'] = compute_product_age(p.get('purchase_date'))
        p_stats = review_stat_map.get(p['product_id'], {'avg_rating': 0.0, 'review_count': 0})
        p['avg_rating'] = p_stats['avg_rating']
        p['review_count'] = p_stats['review_count']

    wishlisted_pids = []
    if student_id:
        try:
            cur.execute("""
                SELECT i.product_id 
                FROM includes i 
                JOIN wishlist w ON i.wishlist_id = w.wishlist_id 
                WHERE w.student_id = %s
            """, (student_id,))
            w_rows = cur.fetchall() or []
            wishlisted_pids = [r['product_id'] for r in w_rows]
        except Exception:
            pass

    # Extract distinct categories and courses for quick filters
    all_categories = set(['Books', 'Electronics', 'Scientific Calculator', 'Lab Equipment', 'Stationery', 'Bicycles', 'Other'])
    for p in available_products + sold_products:
        if p.get('category') and p['category'].strip():
            all_categories.add(p['category'].strip())

    all_courses = set()
    for p in available_products + sold_products:
        raw_c = p.get('used_in_course') or ''
        for c in raw_c.replace(';', ',').replace('/', ',').split(','):
            c_clean = c.strip().upper()
            if c_clean:
                all_courses.add(c_clean)

    sorted_categories = sorted(list(all_categories))
    sorted_courses = sorted(list(all_courses))

    cur.close()
    cart_count = get_cart_count(student_id) if student_id else 0
    wishlist_count = get_wishlist_count(student_id) if student_id else 0

    return render_template(
        'marketplace.html',
        available_products=available_products,
        sold_products=sold_products,
        products=available_products,
        wishlisted_pids=wishlisted_pids,
        cart_count=cart_count,
        wishlist_count=wishlist_count,
        categories=sorted_categories,
        courses=sorted_courses
    )


@app.route('/product/<int:product_id>')
def product_detail(product_id):
    student_id = session.get('student_id')
    cur = mysql.connection.cursor()

    cur.execute("SELECT * FROM product WHERE product_id = %s", (product_id,))
    product = cur.fetchone()

    if not product:
        cur.close()
        return redirect(url_for('marketplace'))

    if product.get('order_id') and not product.get('sold_date'):
        product['sold_date'] = date.today().strftime('%Y-%m-%d')

    cur.execute("SELECT photo FROM product_photo WHERE product_id = %s", (product_id,))
    photo_rows = cur.fetchall() or []
    photos = [r['photo'] for r in photo_rows if r.get('photo')]
    if not photos:
        photos = ['https://images.unsplash.com/photo-1544716278-ca5e3f4abd8c?w=600']

    cur.execute("""
        SELECT student_id, name, department, semester, trust_score 
        FROM user 
        WHERE student_id = %s
    """, (product['student_id'],))
    seller = cur.fetchone()

    seller_trust_score = 5.0
    total_reviews = 0
    seller_reviews = []
    product_reviews = []
    product_avg_rating = 0.0
    product_reviews_count = 0

    # 1. Fetch reviews specifically left for THIS product
    cur.execute("""
        SELECT r.r_id, r.rating, r.comment, r.review_date, r.order_id,
               u.name as reviewer_name, u.student_id as reviewer_student_id,
               COALESCE(p.product_name, JSON_UNQUOTE(JSON_EXTRACT(o.receipt, '$.items[0].product_name')), 'This Product') as product_name
        FROM review r
        JOIN user u ON r.reviewer_id = u.student_id
        JOIN orders o ON r.order_id = o.order_id
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN added a ON a.cart_id = o.cart_id
        WHERE p.product_id = %s 
           OR JSON_EXTRACT(o.receipt, '$.items[0].product_id') = %s
           OR a.product_id = %s
        GROUP BY r.r_id
        ORDER BY r.review_date DESC
    """, (product_id, product_id, product_id))
    product_reviews = cur.fetchall() or []
    product_reviews_count = len(product_reviews)
    if product_reviews_count > 0:
        product_avg_rating = round(sum([float(r['rating'] or 5) for r in product_reviews]) / product_reviews_count, 1)

    # 2. Fetch overall seller reputation and seller other reviews
    if seller:
        cur.execute("""
            SELECT AVG(rating) as avg_rating, COUNT(r_id) as total_reviews 
            FROM review 
            WHERE reviewee_id = %s
        """, (seller['student_id'],))
        t_data = cur.fetchone()
        if t_data and t_data['total_reviews'] and t_data['total_reviews'] > 0:
            seller_trust_score = float(t_data['avg_rating'])
            total_reviews = int(t_data['total_reviews'])
        elif seller.get('trust_score'):
            seller_trust_score = float(seller['trust_score'])
            total_reviews = 1 if seller_trust_score > 0 else 0

        # Fetch other reviews for this seller
        cur.execute("""
            SELECT r.r_id, r.rating, r.comment, r.review_date, r.order_id,
                   u.name as reviewer_name, u.student_id as reviewer_student_id,
                   p.product_name
            FROM review r
            JOIN user u ON r.reviewer_id = u.student_id
            LEFT JOIN orders o ON r.order_id = o.order_id
            LEFT JOIN product p ON p.order_id = o.order_id
            WHERE r.reviewee_id = %s
            ORDER BY r.review_date DESC
            LIMIT 12
        """, (seller['student_id'],))
        seller_reviews = cur.fetchall() or []

    seller_other_reviews = [r for r in seller_reviews if r['r_id'] not in [pr['r_id'] for pr in product_reviews]]

    cur.close()

    product_age = compute_product_age(product.get('purchase_date'))
    cart_count = get_cart_count(student_id) if student_id else 0

    return render_template(
        'product_detail.html',
        product=product,
        photos=photos,
        seller=seller,
        seller_trust_score=seller_trust_score,
        total_reviews=total_reviews,
        product_reviews=product_reviews,
        product_avg_rating=product_avg_rating,
        product_reviews_count=product_reviews_count,
        seller_other_reviews=seller_other_reviews,
        product_age=product_age,
        cart_count=cart_count
    )


def match_and_notify_wishlists(cur, seller_id, seller_name, product_id, product_name, category, used_in_course, selling_price):
    """
    Compares newly uploaded product with all students' permanent wishlists.
    Matches across multiple courses (e.g. CSE260 in CSE260, CSE250, CSE251)
    and keywords (e.g. 'Jump Wires'), sending instant notifications to students.
    """
    if not product_name:
        return

    # Normalize seller courses and extract 3-letter departmental course prefixes
    seller_courses = set([c.strip().upper() for c in (used_in_course or '').replace(';', ',').replace('/', ',').split(',') if c.strip()])
    seller_prefixes = extract_course_prefixes(used_in_course)
    
    # Tokenize product name into clean keywords
    seller_tokens = set([t.lower() for t in product_name.replace('-', ' ').replace('/', ' ').replace('(', ' ').replace(')', ' ').split() if len(t) >= 3])

    notified_students = set()

    # 1. Compare with Custom Wishlist Requests (wishlist_custom_item)
    try:
        ensure_db_schema()
        cur.execute("""
            SELECT item_id, wishlist_id, student_id, item_name, category, used_in_course, target_price
            FROM wishlist_custom_item
            WHERE student_id != %s
        """, (seller_id,))
        custom_wishes = cur.fetchall() or []

        for wish in custom_wishes:
            st_id = wish['student_id']
            if st_id in notified_students:
                continue

            wish_courses = set([c.strip().upper() for c in (wish.get('used_in_course') or '').replace(';', ',').replace('/', ',').split(',') if c.strip()])
            wish_prefixes = extract_course_prefixes(wish.get('used_in_course'))
            wish_name = (wish.get('item_name') or '').strip().lower()
            wish_tokens = set([t for t in wish_name.replace('-', ' ').replace('/', ' ').replace('(', ' ').replace(')', ' ').split() if len(t) >= 3])

            # Check 1st 3 letters course prefix match (e.g. CSE matching in CSE110, CSE220, CSE260)
            prefix_matched = bool(seller_prefixes and wish_prefixes and (seller_prefixes & wish_prefixes))
            matched_prefixes_str = ", ".join(sorted(seller_prefixes & wish_prefixes)) if prefix_matched else ""

            # Check exact course overlap as well
            exact_course_matched = bool(seller_courses and wish_courses and (seller_courses & wish_courses))

            # Check title similarity / keyword overlap
            name_matched = False
            if wish_name and (wish_name in product_name.lower() or product_name.lower() in wish_name):
                name_matched = True
            elif wish_tokens and seller_tokens and (wish_tokens & seller_tokens):
                name_matched = True

            category_matched = bool(category and wish.get('category') and category.lower() == wish['category'].lower())

            if prefix_matched or exact_course_matched or name_matched or (prefix_matched and category_matched):
                match_reason = ""
                if prefix_matched and name_matched:
                    match_reason = f"matching '{wish['item_name']}' for course prefix {matched_prefixes_str}"
                elif prefix_matched:
                    match_reason = f"recommended for course prefix {matched_prefixes_str} ({used_in_course}) which you wishlisted"
                elif name_matched:
                    match_reason = f"matching your wishlist item '{wish['item_name']}'"
                elif exact_course_matched:
                    match_reason = f"listed for course {used_in_course} which you requested"

                notif_text = f"⚡ Course Recommendation Alert: A peer just listed '{product_name}' (৳{selling_price:.0f}) {match_reason}!"

                try:
                    cur.execute("""
                        INSERT INTO notification 
                        (buyer_notification, user_notification, text, notification_type, notification_status)
                        VALUES (%s, %s, %s, 'wishlist_match', 'unread')
                    """, (st_id, seller_id, notif_text))
                    notified_students.add(st_id)
                except Exception:
                    pass
    except Exception as e:
        print("Error matching custom wishlists:", e)

    # 2. Compare with Standard Saved Wishlist Items (includes + product)
    try:
        cur.execute("""
            SELECT DISTINCT w.student_id, w.wishlist_id, p.product_name, p.used_in_course, p.category
            FROM wishlist w
            JOIN includes i ON w.wishlist_id = i.wishlist_id
            JOIN product p ON i.product_id = p.product_id
            WHERE w.student_id != %s
        """, (seller_id,))
        standard_wishes = cur.fetchall() or []

        for sw in standard_wishes:
            st_id = sw['student_id']
            if st_id in notified_students:
                continue

            sw_courses = set([c.strip().upper() for c in (sw.get('used_in_course') or '').replace(';', ',').replace('/', ',').split(',') if c.strip()])
            sw_prefixes = extract_course_prefixes(sw.get('used_in_course'))
            sw_name = (sw.get('product_name') or '').strip().lower()
            sw_tokens = set([t for t in sw_name.replace('-', ' ').replace('/', ' ').split() if len(t) >= 3])

            # Check 1st 3 letters course prefix match
            prefix_matched = bool(seller_prefixes and sw_prefixes and (seller_prefixes & sw_prefixes))
            matched_prefixes_str = ", ".join(sorted(seller_prefixes & sw_prefixes)) if prefix_matched else ""

            course_matched = bool(seller_courses and sw_courses and (seller_courses & sw_courses))
            name_matched = bool(sw_name in product_name.lower() or product_name.lower() in sw_name or (sw_tokens and seller_tokens and (sw_tokens & seller_tokens)))

            if prefix_matched or course_matched or name_matched:
                if prefix_matched:
                    reason = f"matching your wishlisted {matched_prefixes_str} courses"
                elif used_in_course:
                    reason = f"for {used_in_course}"
                else:
                    reason = "matching your saved items"

                notif_text = f"⚡ Course Recommendation Match: '{product_name}' (৳{selling_price:.0f}) was just listed by {seller_name or 'a peer'} {reason}!"
                try:
                    cur.execute("""
                        INSERT INTO notification 
                        (buyer_notification, user_notification, wishlist_id, text, notification_type, notification_status)
                        VALUES (%s, %s, %s, %s, 'wishlist_match', 'unread')
                    """, (st_id, seller_id, sw.get('wishlist_id'), notif_text))
                    notified_students.add(st_id)
                except Exception:
                    pass
    except Exception as e:
        print("Error matching standard wishlists:", e)


@app.route('/sell', methods=['GET', 'POST'])
def sell_product():
    if not session.get('student_id'):
        return redirect(url_for('login'))

    student_id = session.get('student_id')
    cur = mysql.connection.cursor()

    if request.method == 'POST':
        product_id_input = request.form.get('product_id', '').strip()
        product_name = request.form.get('product_name', '').strip()
        category = request.form.get('category', '').strip()
        description = request.form.get('description', '').strip()
        selling_price = float(request.form.get('selling_price', 0))
        recommended_price_input = request.form.get('recommended_price', '').strip()
        warranty = request.form.get('warranty', '').strip()
        used_in_course = request.form.get('used_in_course', '').strip()
        purchase_date = request.form.get('purchase_date', '').strip() or None
        sold_date = request.form.get('sold_date', '').strip() or None

        if recommended_price_input:
            try:
                recommended_price = float(recommended_price_input)
            except ValueError:
                recommended_price = compute_recommended_price(selling_price, category)
        else:
            recommended_price = compute_recommended_price(selling_price, category)

        if product_id_input and product_id_input.isdigit():
            target_id = int(product_id_input)
            cur.execute("""
                INSERT INTO product 
                (product_id, product_name, category, description, selling_price, 
                 recommended_price, warranty, used_in_course, purchase_date, sold_date, student_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                target_id, product_name, category, description, selling_price,
                recommended_price, warranty, used_in_course, purchase_date, sold_date, student_id
            ))
        else:
            cur.execute("""
                INSERT INTO product 
                (product_name, category, description, selling_price, 
                 recommended_price, warranty, used_in_course, purchase_date, sold_date, student_id)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """, (
                product_name, category, description, selling_price,
                recommended_price, warranty, used_in_course, purchase_date, sold_date, student_id
            ))
            target_id = cur.lastrowid

        # 1. Handle device image file uploads
        uploaded_files = request.files.getlist('product_images')
        for file in uploaded_files:
            if file and file.filename and file.filename.strip():
                orig_filename = secure_filename(file.filename)
                if orig_filename:
                    ext = os.path.splitext(orig_filename)[1].lower()
                    if ext in ['.jpg', '.jpeg', '.png', '.webp', '.gif', '.jfif', '.heic', '.bmp']:
                        unique_filename = f"prod_{target_id}_{int(datetime.now().timestamp())}_{random.randint(100, 999)}{ext}"
                        save_path = os.path.join(app.config['UPLOAD_FOLDER'], unique_filename)
                        file.save(save_path)
                        photo_url = f"/static/uploads/{unique_filename}"
                        try:
                            cur.execute("""
                                INSERT INTO product_photo (product_id, photo)
                                VALUES (%s, %s)
                            """, (target_id, photo_url))
                        except Exception:
                            pass

        # 2. Handle image URLs if provided
        photos = request.form.getlist('photos[]')
        for p_url in photos:
            p_url = p_url.strip()
            if p_url and not p_url.startswith('https://example.com'):
                try:
                    cur.execute("""
                        INSERT INTO product_photo (product_id, photo)
                        VALUES (%s, %s)
                    """, (target_id, p_url))
                except Exception:
                    pass

        try:
            cur.execute("""
                INSERT INTO product_price (product_id, selling)
                VALUES (%s, %s)
            """, (target_id, selling_price))
        except Exception:
            pass

        try:
            cur.execute("""
                INSERT INTO product_date (product_id, purchase, sold)
                VALUES (%s, %s, %s)
            """, (target_id, purchase_date, sold_date))
        except Exception:
            pass

        # Fetch seller's name for friendly notifications
        cur.execute("SELECT name FROM user WHERE student_id = %s", (student_id,))
        seller_row = cur.fetchone()
        seller_name = seller_row['name'] if seller_row else 'A peer'

        # Automatic comparison of newly uploaded product with all students' wishlists
        match_and_notify_wishlists(cur, student_id, seller_name, target_id, product_name, category, used_in_course, selling_price)

        mysql.connection.commit()
        cur.close()

        return redirect(url_for('product_detail', product_id=target_id))

    cur.execute("SELECT COALESCE(MAX(product_id), 0) + 1 AS next_id FROM product")
    row = cur.fetchone()
    next_id = row['next_id'] if row else 1
    cur.close()

    cart_count = get_cart_count(student_id)
    return render_template('sell_product.html', next_id=next_id, cart_count=cart_count)





# =========================================================================
# CART & CHECKOUT ROUTES
# =========================================================================

@app.route('/cart')
def cart_view():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT p.product_id, p.product_name, p.category, p.selling_price, 
               p.used_in_course, u.name as seller_name,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
        FROM cart c
        JOIN added a ON c.cart_id = a.cart_id
        JOIN product p ON a.product_id = p.product_id
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE c.student_id = %s
        ORDER BY p.product_id DESC
    """, (student_id,))
    cart_items = list(cur.fetchall() or [])

    # Attach product-specific review ratings to cart items
    cur.execute("""
        SELECT COALESCE(p.product_id, JSON_UNQUOTE(JSON_EXTRACT(o.receipt, '$.items[0].product_id')), a.product_id) as pid,
               AVG(r.rating) as avg_rating, COUNT(DISTINCT r.r_id) as review_count
        FROM review r
        JOIN orders o ON r.order_id = o.order_id
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN added a ON a.cart_id = o.cart_id
        GROUP BY pid
    """)
    review_stat_rows = cur.fetchall() or []
    review_stat_map = {}
    for rs in review_stat_rows:
        if rs.get('pid'):
            try:
                review_stat_map[int(rs['pid'])] = {
                    'avg_rating': round(float(rs['avg_rating']), 1),
                    'review_count': int(rs['review_count'])
                }
            except Exception:
                pass

    for item in cart_items:
        stats = review_stat_map.get(item['product_id'], {'avg_rating': 0.0, 'review_count': 0})
        item['avg_rating'] = stats['avg_rating']
        item['review_count'] = stats['review_count']

    total_bill = sum(float(item['selling_price'] or 0) for item in cart_items)

    # Course Code Recommendation Engine (1st 3 letters departmental prefix matching)
    cart_pids = [item['product_id'] for item in cart_items]
    tracked_courses = [item['used_in_course'].strip().upper() for item in cart_items if item.get('used_in_course') and item.get('used_in_course').strip()]
    tracked_courses = list(dict.fromkeys(tracked_courses))  # Unique list
    cart_prefixes = set()
    for c in tracked_courses:
        cart_prefixes.update(extract_course_prefixes(c))

    recommendations = []
    if cart_prefixes:
        pfx_clauses = ["UPPER(p.used_in_course) LIKE %s" for _ in cart_prefixes]
        or_pfx = " OR ".join(pfx_clauses)
        params = [f"%{pfx}%" for pfx in cart_prefixes]
        query = f"""
            SELECT p.product_id, p.product_name, p.category, p.selling_price, 
                   p.recommended_price, p.used_in_course, u.name as seller_name,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
            FROM product p
            LEFT JOIN user u ON p.student_id = u.student_id
            WHERE p.sold_date IS NULL
              AND ({or_pfx})
        """
        if cart_pids:
            p_format = ','.join(['%s'] * len(cart_pids))
            query += f" AND p.product_id NOT IN ({p_format})"
            params.extend(cart_pids)
        query += " ORDER BY p.product_id DESC LIMIT 12"
        cur.execute(query, tuple(params))
        rows = list(cur.fetchall() or [])
        for r in rows:
            if extract_course_prefixes(r.get('used_in_course')) & cart_prefixes:
                recommendations.append(r)
        recommendations = recommendations[:6]

    cur.close()

    return render_template(
        'cart.html',
        cart_items=cart_items,
        total_bill=round(total_bill, 2),
        cart_count=len(cart_items),
        recommendations=recommendations,
        tracked_courses=tracked_courses
    )


@app.route('/add-to-cart/<int:product_id>', methods=['GET', 'POST'])
def add_to_cart_route(product_id):
    student_id = session.get('student_id')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json

    if not student_id:
        if is_ajax:
            return jsonify({'success': False, 'redirect': url_for('login')}), 401
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()
    cur.execute("SELECT sold_date, order_id, product_name FROM product WHERE product_id = %s", (product_id,))
    p_check = cur.fetchone()
    if not p_check:
        cur.close()
        if is_ajax or request.method == 'POST':
            return jsonify({'success': False, 'message': 'Product not found.'}), 404
        return redirect(url_for('marketplace'))

    if p_check.get('sold_date') or p_check.get('order_id'):
        cur.close()
        if is_ajax or request.method == 'POST':
            return jsonify({'success': False, 'message': f"'{p_check.get('product_name', 'Item')}' has already been sold."}), 400
        return redirect(url_for('marketplace'))

    cur.execute("SELECT cart_id FROM cart WHERE student_id = %s LIMIT 1", (student_id,))
    user_cart = cur.fetchone()
    if not user_cart:
        cur.execute("INSERT INTO cart (student_id, total_bill) VALUES (%s, 0)", (student_id,))
        mysql.connection.commit()
        cart_id = cur.lastrowid
    else:
        cart_id = user_cart['cart_id']

    try:
        cur.execute("INSERT IGNORE INTO added (cart_id, product_id) VALUES (%s, %s)", (cart_id, product_id))
    except Exception:
        pass

    try:
        cur.execute("INSERT IGNORE INTO add_to_cart (product_id, student_id) VALUES (%s, %s)", (product_id, student_id))
    except Exception:
        pass

    cur.execute("SELECT COUNT(product_id) as cnt FROM added WHERE cart_id = %s", (cart_id,))
    count_row = cur.fetchone()
    new_count = int(count_row['cnt']) if count_row else 0

    mysql.connection.commit()
    cur.close()

    if is_ajax or request.method == 'POST':
        return jsonify({
            'success': True,
            'cart_count': new_count,
            'message': f'Item added to cart! Total: {new_count} item(s).'
        })

    return redirect(url_for('cart_view'))


@app.route('/remove-from-cart/<int:product_id>')
def remove_from_cart(product_id):
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()
    cur.execute("SELECT cart_id FROM cart WHERE student_id = %s LIMIT 1", (student_id,))
    user_cart = cur.fetchone()
    if user_cart:
        cart_id = user_cart['cart_id']
        cur.execute("DELETE FROM added WHERE cart_id = %s AND product_id = %s", (cart_id, product_id))
        cur.execute("DELETE FROM add_to_cart WHERE student_id = %s AND product_id = %s", (student_id, product_id))
        mysql.connection.commit()
    cur.close()

    return redirect(url_for('cart_view'))


@app.route('/cart/checkout', methods=['POST'])
def cart_checkout():
    student_id = session.get('student_id')
    user_name = session.get('user_name', 'Student Buyer')

    if not student_id:
        return redirect(url_for('login'))

    delivery_place = request.form.get('delivery_place', 'UB Gate / Building Lobby')
    payment_method = request.form.get('payment_method', 'bkash')
    account_number = request.form.get('account_number', '').strip()
    trx_id = request.form.get('trx_id', '').strip()

    cur = mysql.connection.cursor()

    cur.execute("SELECT cart_id FROM cart WHERE student_id = %s LIMIT 1", (student_id,))
    user_cart = cur.fetchone()
    if not user_cart:
        cur.close()
        return redirect(url_for('marketplace'))

    cart_id = user_cart['cart_id']

    cur.execute("""
        SELECT p.product_id, p.product_name, p.selling_price, p.student_id as seller_id, u.name as seller_name, p.sold_date
        FROM added a 
        JOIN product p ON a.product_id = p.product_id 
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE a.cart_id = %s
    """, (cart_id,))
    items = cur.fetchall() or []

    if not items:
        cur.close()
        return redirect(url_for('cart_view'))

    total_bill = sum(float(i['selling_price'] or 0) for i in items)
    is_digital_paid = payment_method in ['bkash', 'nagad', 'rocket', 'card']
    delivery_status = 'confirmed' if is_digital_paid else 'pending'
    buyer_confirmation = 1 if is_digital_paid else 0
    confirmation = 1 if is_digital_paid else 0

    cur.execute("""
        INSERT INTO orders 
        (cart_id, st_buyer_id, final_bill, payment_method, delivery_date, delivery_place, delivery_status, buyer_confirmation, confirmation, order_type)
        VALUES (%s, %s, %s, %s, CURDATE(), %s, %s, %s, %s, 'buy')
    """, (cart_id, student_id, total_bill, payment_method, delivery_place, delivery_status, buyer_confirmation, confirmation))
    order_id = cur.lastrowid

    # Generate Digital Receipt
    items_summary = [{'product_id': i['product_id'], 'product_name': i['product_name'], 'price': float(i['selling_price'] or 0), 'seller_name': i.get('seller_name', 'Peer')} for i in items]
    receipt_json = create_digital_receipt(order_id, student_id, user_name, total_bill, payment_method, account_number, trx_id, delivery_place, items_summary)
    cur.execute("UPDATE orders SET receipt = %s WHERE order_id = %s", (receipt_json, order_id))

    cur.execute("UPDATE cart SET order_id = %s, total_bill = %s WHERE cart_id = %s", (order_id, total_bill, cart_id))

    # Mark items as sold with order_id and sold_date
    for item in items:
        cur.execute("UPDATE product SET order_id = %s, sold_date = CURDATE() WHERE product_id = %s", (order_id, item['product_id']))
        try:
            cur.execute("UPDATE product_date SET sold = CURDATE() WHERE product_id = %s", (item['product_id'],))
        except Exception:
            pass

        # Send notification to seller
        seller_id = item.get('seller_id')
        if seller_id and seller_id != student_id:
            msg = f"Order #{order_id}: {user_name} placed an order for '{item['product_name']}' (৳{item['selling_price']}) via {payment_method.replace('_', ' ').upper()}."
            try:
                cur.execute("""
                    INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
                    VALUES (%s, %s, %s, 'payment_received', 'unread')
                """, (seller_id, student_id, msg))
            except Exception:
                pass

    # Send notification to buyer
    buyer_msg = f"Order #{order_id} confirmed! Total: ৳{total_bill} via {payment_method.replace('_', ' ').upper()}. Delivery at {delivery_place}."
    try:
        cur.execute("""
            INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
            VALUES (%s, %s, %s, 'order_confirmed', 'unread')
        """, (student_id, student_id, buyer_msg))
    except Exception:
        pass

    # Clear current cart added items for next shopping session
    cur.execute("DELETE FROM added WHERE cart_id = %s", (cart_id,))

    mysql.connection.commit()
    cur.close()

    return redirect(url_for('profile'))


@app.route('/buy-now/<int:product_id>', methods=['POST'])
def buy_now(product_id):
    student_id = session.get('student_id')
    user_name = session.get('user_name', 'Student Buyer')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json

    if not student_id:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please login to purchase.', 'redirect': url_for('login')}), 401
        return redirect(url_for('login'))

    delivery_place = request.form.get('delivery_place', 'UB Gate / Building Lobby')
    payment_method = request.form.get('payment_method', 'bkash')
    account_number = request.form.get('account_number', '').strip()
    trx_id = request.form.get('trx_id', '').strip()

    cur = mysql.connection.cursor()

    cur.execute("""
        SELECT p.product_id, p.product_name, p.selling_price, p.student_id as seller_id, u.name as seller_name, p.sold_date
        FROM product p
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE p.product_id = %s
    """, (product_id,))
    product = cur.fetchone()

    if not product:
        cur.close()
        if is_ajax:
            return jsonify({'success': False, 'message': 'Product not found.'}), 404
        return redirect(url_for('marketplace'))

    # Ensure student cart exists
    try:
        cur.execute("SELECT cart_id FROM cart WHERE student_id = %s LIMIT 1", (student_id,))
        user_cart = cur.fetchone()
        if not user_cart:
            cur.execute("INSERT INTO cart (student_id, total_bill) VALUES (%s, 0)", (student_id,))
            mysql.connection.commit()
            cart_id = cur.lastrowid
        else:
            cart_id = user_cart['cart_id']

        total_bill = float(product['selling_price'] or 0)
        is_digital_paid = payment_method in ['bkash', 'nagad', 'rocket', 'card']
        delivery_status = 'confirmed' if is_digital_paid else 'pending'
        buyer_confirmation = 1 if is_digital_paid else 0
        confirmation = 1 if is_digital_paid else 0

        cur.execute("""
            INSERT INTO orders 
            (cart_id, st_buyer_id, final_bill, payment_method, delivery_date, delivery_place, delivery_status, buyer_confirmation, confirmation, order_type)
            VALUES (%s, %s, %s, %s, CURDATE(), %s, %s, %s, %s, 'buy')
        """, (cart_id, student_id, total_bill, payment_method, delivery_place, delivery_status, buyer_confirmation, confirmation))
        order_id = cur.lastrowid

        # Generate Digital Receipt
        items_summary = [{'product_id': product['product_id'], 'product_name': product['product_name'], 'price': total_bill, 'seller_name': product.get('seller_name', 'Peer')}]
        receipt_json = create_digital_receipt(order_id, student_id, user_name, total_bill, payment_method, account_number, trx_id, delivery_place, items_summary)
        cur.execute("UPDATE orders SET receipt = %s WHERE order_id = %s", (receipt_json, order_id))

        # Link product with order_id and mark as sold
        cur.execute("UPDATE product SET order_id = %s, sold_date = CURDATE() WHERE product_id = %s", (order_id, product_id))
        try:
            cur.execute("UPDATE product_date SET sold = CURDATE() WHERE product_id = %s", (product_id,))
        except Exception:
            pass

        # Notifications
        seller_id = product.get('seller_id')
        if seller_id and seller_id != student_id:
            msg = f"Order #{order_id}: {user_name} purchased '{product['product_name']}' (৳{total_bill}) via {payment_method.replace('_', ' ').upper()}."
            try:
                cur.execute("""
                    INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
                    VALUES (%s, %s, %s, 'payment_received', 'unread')
                """, (seller_id, student_id, msg))
            except Exception:
                pass

        buyer_msg = f"Order #{order_id} confirmed! ৳{total_bill} for '{product['product_name']}' via {payment_method.replace('_', ' ').upper()}. Handover at {delivery_place}."
        try:
            cur.execute("""
                INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
                VALUES (%s, %s, %s, 'order_confirmed', 'unread')
            """, (student_id, student_id, buyer_msg))
        except Exception:
            pass

        mysql.connection.commit()
        cur.close()

        if is_ajax:
            return jsonify({
                'success': True,
                'message': f"Order #{order_id} confirmed via {payment_method.replace('_', ' ').upper()}! Digital receipt generated.",
                'order_id': order_id,
                'redirect': url_for('profile')
            })

        return redirect(url_for('profile'))

    except Exception as e:
        mysql.connection.rollback()
        cur.close()
        if is_ajax:
            return jsonify({'success': False, 'message': f'Payment processing error: {str(e)}'}), 500
        return redirect(url_for('product_detail', product_id=product_id))


@app.route('/pay-order/<int:order_id>', methods=['POST'])
def pay_order(order_id):
    student_id = session.get('student_id')
    user_name = session.get('user_name', 'Student Buyer')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json

    if not student_id:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please login.'}), 401
        return redirect(url_for('login'))

    payment_method = request.form.get('payment_method', 'bkash')
    account_number = request.form.get('account_number', '').strip()
    trx_id = request.form.get('trx_id', '').strip()

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT o.order_id, o.final_bill, o.st_buyer_id, o.delivery_place,
               p.product_id, p.product_name, p.selling_price, p.student_id as seller_id, u.name as seller_name
        FROM orders o
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE o.order_id = %s AND o.st_buyer_id = %s
    """, (order_id, student_id))
    order_rows = cur.fetchall() or []

    if not order_rows:
        cur.close()
        if is_ajax:
            return jsonify({'success': False, 'message': 'Order not found.'}), 404
        return redirect(url_for('profile'))

    order_info = order_rows[0]
    total_bill = float(order_info['final_bill'] or 0)
    items_summary = [{'product_id': r['product_id'], 'product_name': r['product_name'] or 'Campus Item', 'price': float(r['selling_price'] or 0), 'seller_name': r.get('seller_name', 'Peer')} for r in order_rows if r.get('product_id')]

    receipt_json = create_digital_receipt(order_id, student_id, user_name, total_bill, payment_method, account_number, trx_id, order_info['delivery_place'], items_summary)

    cur.execute("""
        UPDATE orders 
        SET payment_method = %s, receipt = %s, delivery_status = 'confirmed', buyer_confirmation = 1, confirmation = 1
        WHERE order_id = %s
    """, (payment_method, receipt_json, order_id))

    cur.execute("UPDATE product SET sold_date = CURDATE() WHERE order_id = %s AND sold_date IS NULL", (order_id,))
    try:
        cur.execute("""
            UPDATE product_date pd
            JOIN product p ON pd.product_id = p.product_id
            SET pd.sold = CURDATE()
            WHERE p.order_id = %s AND pd.sold IS NULL
        """, (order_id,))
    except Exception:
        pass

    # Notify sellers
    for r in order_rows:
        seller_id = r.get('seller_id')
        if seller_id and seller_id != student_id:
            try:
                cur.execute("""
                    INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
                    VALUES (%s, %s, %s, 'payment_received', 'unread')
                """, (seller_id, student_id, f"Order #{order_id} payment of ৳{total_bill} received via {payment_method.replace('_', ' ').upper()}!"))
            except Exception:
                pass

    mysql.connection.commit()
    cur.close()

    if is_ajax:
        return jsonify({
            'success': True,
            'message': f"Payment of ৳{total_bill} confirmed via {payment_method.replace('_', ' ').upper()}! Receipt issued.",
            'order_id': order_id,
            'redirect': url_for('profile')
        })

    return redirect(url_for('profile'))


@app.route('/api/order/<int:order_id>/receipt')
def get_order_receipt(order_id):
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False, 'message': 'Unauthorized'}), 401

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT o.order_id, o.st_buyer_id, o.final_bill, o.payment_method, o.delivery_place, 
               o.delivery_date, o.delivery_status, o.receipt,
               p.product_name, p.selling_price, u.name as seller_name
        FROM orders o
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE o.order_id = %s AND (o.st_buyer_id = %s OR p.student_id = %s)
    """, (order_id, student_id, student_id))
    rows = cur.fetchall() or []
    cur.close()

    if not rows:
        return jsonify({'success': False, 'message': 'Receipt not found or access denied.'}), 404

    row = rows[0]
    receipt_data = None
    if row.get('receipt'):
        try:
            receipt_data = json.loads(row['receipt'])
        except Exception:
            receipt_data = {'raw_text': row['receipt']}

    if not receipt_data or not isinstance(receipt_data, dict):
        is_paid = (row.get('payment_method') or '') in ['bkash', 'nagad', 'rocket', 'card']
        receipt_data = {
            'receipt_no': f"CC-REC-{order_id:05d}",
            'order_id': order_id,
            'trx_id': f"TX-{order_id:05d}",
            'payment_method': (row.get('payment_method') or 'cash_on_meetup').replace('_', ' ').title(),
            'payment_method_code': row.get('payment_method') or 'cash_on_meetup',
            'amount': float(row.get('final_bill') or 0),
            'payment_status': 'PAID (Verified)' if is_paid else 'PENDING CASH ON HANDOVER',
            'is_paid': is_paid,
            'delivery_place': row.get('delivery_place') or 'UB Gate / Building Lobby',
            'items': [{'product_name': r.get('product_name') or 'Item', 'price': float(r.get('selling_price') or 0)} for r in rows if r.get('product_name')]
        }

    return jsonify({'success': True, 'receipt': receipt_data})


@app.route('/submit-review', methods=['POST'])
def submit_review():
    """
    Submits or updates a peer review for a purchased product/order.
    Strictly enforces that only verified buyers who bought the product can review.
    """
    reviewer_id = session.get('student_id')
    if not reviewer_id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
            return jsonify({'success': False, 'message': 'Please login to submit a review.'}), 401
        return redirect(url_for('login'))

    order_id = request.form.get('order_id')
    rating = request.form.get('rating')
    comment = request.form.get('comment', '').strip()

    if not order_id or not rating:
        return jsonify({'success': False, 'message': 'Order ID and Star Rating are required.'}), 400

    try:
        rating = int(rating)
        if rating < 1 or rating > 5:
            return jsonify({'success': False, 'message': 'Rating must be between 1 and 5 stars.'}), 400
    except (ValueError, TypeError):
        return jsonify({'success': False, 'message': 'Invalid rating value.'}), 400

    cur = mysql.connection.cursor()

    # 1. VERIFY: Did this student actually buy this product/order?
    cur.execute("""
        SELECT o.order_id, o.st_buyer_id, o.final_bill, o.receipt,
               p.product_id, p.product_name, p.student_id as seller_id,
               u.name as buyer_name
        FROM orders o
        LEFT JOIN product p ON p.order_id = o.order_id
        LEFT JOIN user u ON u.student_id = o.st_buyer_id
        WHERE o.order_id = %s AND o.st_buyer_id = %s
        LIMIT 1
    """, (order_id, reviewer_id))
    order_row = cur.fetchone()

    if not order_row:
        cur.close()
        return jsonify({
            'success': False, 
            'message': 'Permission denied: You can only review products/sellers after completing a purchase.'
        }), 403

    seller_id = order_row.get('seller_id')
    if not seller_id:
        # Fallback 1: Parse from receipt items if stored in JSON
        if order_row.get('receipt'):
            try:
                rec = json.loads(order_row['receipt'])
                if rec.get('items') and len(rec['items']) > 0:
                    first_pid = rec['items'][0].get('product_id')
                    if first_pid:
                        cur.execute("SELECT student_id as seller_id, product_name FROM product WHERE product_id = %s", (first_pid,))
                        p_row = cur.fetchone()
                        if p_row:
                            seller_id = p_row['seller_id']
                            if not order_row.get('product_name'):
                                order_row['product_name'] = p_row['product_name']
            except Exception:
                pass

    if not seller_id:
        # Fallback 2: find seller from cart items if product.order_id was not populated directly
        cur.execute("""
            SELECT p.student_id as seller_id, p.product_name
            FROM orders o
            JOIN cart c ON o.cart_id = c.cart_id
            JOIN added a ON c.cart_id = a.cart_id
            JOIN product p ON a.product_id = p.product_id
            WHERE o.order_id = %s
            LIMIT 1
        """, (order_id,))
        p_row = cur.fetchone()
        if p_row:
            seller_id = p_row['seller_id']
            if not order_row.get('product_name'):
                order_row['product_name'] = p_row['product_name']

    if not seller_id:
        cur.close()
        return jsonify({'success': False, 'message': 'Seller could not be identified for this order.'}), 400

    if seller_id == reviewer_id:
        cur.close()
        return jsonify({'success': False, 'message': 'You cannot review your own listing.'}), 400

    # 2. Insert or update review record
    cur.execute("SELECT r_id FROM review WHERE order_id = %s AND reviewer_id = %s", (order_id, reviewer_id))
    existing_review = cur.fetchone()

    if existing_review:
        cur.execute("""
            UPDATE review
            SET rating = %s, comment = %s, review_date = NOW()
            WHERE r_id = %s
        """, (rating, comment, existing_review['r_id']))
    else:
        cur.execute("""
            INSERT INTO review (reviewer_id, reviewee_id, rating, comment, order_id)
            VALUES (%s, %s, %s, %s, %s)
        """, (reviewer_id, seller_id, rating, comment, order_id))

    # 3. Recalculate seller's trust score derived from all reviews
    cur.execute("SELECT AVG(rating) as avg_rating FROM review WHERE reviewee_id = %s", (seller_id,))
    avg_data = cur.fetchone()
    if avg_data and avg_data['avg_rating']:
        new_trust_score = round(float(avg_data['avg_rating']), 1)
        cur.execute("UPDATE user SET trust_score = %s WHERE student_id = %s", (new_trust_score, seller_id))

    # 4. Notify seller of review
    buyer_name = order_row.get('buyer_name') or reviewer_id
    prod_name = order_row.get('product_name') or 'purchased item'
    star_str = '★' * rating
    notif_text = f"⭐ {buyer_name} left you a {rating}-star review ({star_str}) for Order #{order_id} ({prod_name}): \"{comment[:50]}\""
    
    try:
        cur.execute("""
            INSERT INTO notification (buyer_notification, user_notification, text, notification_type, notification_status)
            VALUES (%s, %s, %s, 'review_received', 'unread')
        """, (seller_id, reviewer_id, notif_text))
    except Exception:
        pass

    mysql.connection.commit()
    cur.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        return jsonify({
            'success': True,
            'message': 'Thank you! Your verified purchase review has been recorded.',
            'rating': rating,
            'comment': comment
        })

    return redirect(url_for('profile'))


# =========================================================================
# WISHLIST & NOTIFICATION APPLICATION ROUTES (WISHLIST, INCLUDES, NOTIFICATION)
# =========================================================================

@app.route('/wishlist')
def wishlist_view():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()

    # 1. Fetch or initialize wishlist record for this student
    cur.execute("SELECT wishlist_id, recommendation FROM wishlist WHERE student_id = %s LIMIT 1", (student_id,))
    wishlist_row = cur.fetchone()
    if not wishlist_row:
        cur.execute("INSERT INTO wishlist (student_id, recommendation) VALUES (%s, %s)", (student_id, ''))
        mysql.connection.commit()
        cur.execute("SELECT wishlist_id, recommendation FROM wishlist WHERE student_id = %s LIMIT 1", (student_id,))
        wishlist_row = cur.fetchone()

    wishlist_id = wishlist_row['wishlist_id']
    stored_recommendation = wishlist_row['recommendation'] or ''

    # 2. Fetch all products currently saved in the user's wishlist
    cur.execute("""
        SELECT p.product_id, p.product_name, p.category, p.description,
               p.selling_price, p.recommended_price, p.used_in_course,
               p.sold_date, p.order_id, p.student_id AS seller_id, u.name AS seller_name,
               u.trust_score AS seller_trust_score,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo
        FROM includes i
        JOIN product p ON i.product_id = p.product_id
        JOIN user u ON p.student_id = u.student_id
        WHERE i.wishlist_id = %s
        ORDER BY p.product_id DESC
    """, (wishlist_id,))
    wishlist_items = cur.fetchall() or []
    for item in wishlist_items:
        if item.get('order_id') and not item.get('sold_date'):
            item['sold_date'] = date.today().strftime('%Y-%m-%d')

    wishlisted_product_ids = [item['product_id'] for item in wishlist_items]

    # 3. Fetch Custom Requested Wishlist Items (BEFORE computing recommendations, so course codes are captured!)
    ensure_db_schema()
    cur.execute("""
        SELECT item_id, wishlist_id, student_id, item_name, category, 
               target_price, used_in_course, notes, status, created_at
        FROM wishlist_custom_item
        WHERE student_id = %s OR wishlist_id = %s
        ORDER BY item_id DESC
    """, (student_id, wishlist_id))
    custom_items = cur.fetchall() or []

    # Track all course codes and 3-letter departmental course prefixes
    tracked_courses_set = set()
    wishlist_prefixes = set()

    for item in wishlist_items:
        raw_c = item.get('used_in_course') or ''
        for c in raw_c.replace(';', ',').replace('/', ',').split(','):
            c_clean = c.strip().upper()
            if c_clean:
                tracked_courses_set.add(c_clean)
        wishlist_prefixes.update(extract_course_prefixes(raw_c))

    # Gather all course codes and 3-letter departmental course prefixes from custom wish requests
    for ci in custom_items:
        raw_courses = ci.get('used_in_course') or ''
        courses_list = [c.strip().upper() for c in raw_courses.replace(';', ',').replace('/', ',').split(',') if c.strip()]
        for c_clean in courses_list:
            tracked_courses_set.add(c_clean)
        ci['courses_list'] = courses_list
        ci_prefixes = extract_course_prefixes(raw_courses)
        wishlist_prefixes.update(ci_prefixes)

    wishlisted_courses = list(tracked_courses_set)

    # 4. Intelligent Course & Marketplace Recommendations matching 1st 3 letters of course code
    # Displayed in the main recommendations section for all tracked course codes from wishlist & custom requests
    recommendations = []
    if wishlist_prefixes:
        pfx_clauses = ["UPPER(p.used_in_course) LIKE %s" for _ in wishlist_prefixes]
        where_pfx = " OR ".join(pfx_clauses)
        params = [f"%{pfx}%" for pfx in wishlist_prefixes]

        query = f"""
            SELECT DISTINCT p.product_id, p.product_name, p.category, p.description,
                   p.selling_price, p.recommended_price, p.used_in_course, p.student_id,
                   u.name AS seller_name, u.trust_score AS seller_trust_score,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo
            FROM product p
            JOIN user u ON p.student_id = u.student_id
            WHERE p.sold_date IS NULL
              AND ({where_pfx})
        """
        params_list = list(params)
        if wishlisted_product_ids:
            p_format = ','.join(['%s'] * len(wishlisted_product_ids))
            query += f" AND p.product_id NOT IN ({p_format})"
            params_list += wishlisted_product_ids

        # Prioritize listings from peer students, but also allow student's listings if testing locally
        query += " ORDER BY (p.student_id != %s) DESC, p.product_id DESC LIMIT 24"
        cur.execute(query, tuple(params_list + [student_id]))
        candidate_rows = list(cur.fetchall() or [])

        seen_pids = set()
        for row in candidate_rows:
            if row['product_id'] in seen_pids:
                continue
            row_pfxs = extract_course_prefixes(row.get('used_in_course'))
            matching_pfxs = row_pfxs & wishlist_prefixes
            if matching_pfxs:
                pfx_matched = sorted(list(matching_pfxs))[0]
                row['reason_type'] = "Course Match:"
                row['reason_value'] = f"{pfx_matched} ({row.get('used_in_course')})"
                row['matched_prefix'] = pfx_matched
                recommendations.append(row)
                seen_pids.add(row['product_id'])

        if recommendations:
            pfx_str = ", ".join(sorted(wishlist_prefixes))
            rec_summary = f"Smart recommendations for {pfx_str}: " + ", ".join([r['product_name'] for r in recommendations[:4]])
            try:
                cur.execute("UPDATE wishlist SET recommendation = %s WHERE wishlist_id = %s", (rec_summary, wishlist_id))
                mysql.connection.commit()
                stored_recommendation = rec_summary
            except Exception:
                pass

    # Fallback only if absolutely no course recommendations found
    if not recommendations:
        exclude_ids = wishlisted_product_ids
        fallback_query = """
            SELECT DISTINCT p.product_id, p.product_name, p.category, p.description,
                   p.selling_price, p.recommended_price, p.used_in_course,
                   u.name AS seller_name, u.trust_score AS seller_trust_score,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo,
                   'Popular on Campus' AS reason_type, p.category AS reason_value
            FROM product p
            JOIN user u ON p.student_id = u.student_id
            WHERE p.sold_date IS NULL
        """
        params = []
        if exclude_ids:
            p_format = ','.join(['%s'] * len(exclude_ids))
            fallback_query += f" AND p.product_id NOT IN ({p_format})"
            params += exclude_ids
        fallback_query += " ORDER BY p.product_id DESC LIMIT 8"
        cur.execute(fallback_query, tuple(params))
        recommendations = list(cur.fetchall() or [])

    # 5. Fetch Archived Sold Marketplace Products (for adding sold items to wishlist)
    cur.execute("""
        SELECT p.product_id, p.product_name, p.category, p.selling_price, 
               p.used_in_course, p.sold_date, u.name as seller_name,
               (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
        FROM product p
        LEFT JOIN user u ON p.student_id = u.student_id
        WHERE p.sold_date IS NOT NULL
        ORDER BY p.sold_date DESC, p.product_id DESC
        LIMIT 24
    """)
    archived_sold_products = cur.fetchall() or []

    cart_count = get_cart_count(student_id)
    wishlist_count = len(wishlist_items) + len(custom_items)

    cur.close()

    return render_template(
        'wishlist.html',
        wishlist_items=wishlist_items,
        custom_items=custom_items,
        archived_sold_products=archived_sold_products,
        wishlisted_product_ids=wishlisted_product_ids,
        recommendations=recommendations,
        wishlisted_courses=wishlisted_courses,
        wishlist_prefixes=sorted(list(wishlist_prefixes)),
        cart_count=cart_count,
        wishlist_count=wishlist_count,
        stored_recommendation=stored_recommendation
    )


@app.route('/wishlist/add-custom', methods=['POST'])
def add_custom_wishlist_item():
    student_id = session.get('student_id')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
    if not student_id:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please login first.', 'redirect': url_for('login')}), 401
        return redirect(url_for('login'))

    ensure_db_schema()

    item_name = request.form.get('item_name', '').strip()
    category = request.form.get('category', 'Books').strip()
    raw_courses = request.form.get('used_in_course', '').strip()
    courses_list = [c.strip().upper() for c in raw_courses.replace(';', ',').replace('/', ',').split(',') if c.strip()]
    used_in_course = ", ".join(courses_list) if courses_list else None
    target_price_raw = request.form.get('target_price', '').strip()
    notes = request.form.get('notes', '').strip()

    if not item_name:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please provide an item or product name.'}), 400
        return redirect(url_for('wishlist_view'))

    target_price = None
    if target_price_raw:
        try:
            target_price = float(target_price_raw)
        except ValueError:
            target_price = None

    cur = mysql.connection.cursor()
    cur.execute("SELECT wishlist_id FROM wishlist WHERE student_id = %s LIMIT 1", (student_id,))
    w_row = cur.fetchone()
    if not w_row:
        cur.execute("INSERT INTO wishlist (student_id, recommendation) VALUES (%s, %s)", (student_id, ''))
        mysql.connection.commit()
        wishlist_id = cur.lastrowid
    else:
        wishlist_id = w_row['wishlist_id']

    cur.execute("""
        INSERT INTO wishlist_custom_item 
        (wishlist_id, student_id, item_name, category, target_price, used_in_course, notes)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
    """, (wishlist_id, student_id, item_name, category, target_price, used_in_course, notes))

    # Check for immediate matching active marketplace products matching 1st 3 letters of course code
    matching_listings = []
    custom_prefixes = extract_course_prefixes(raw_courses)
    if custom_prefixes:
        try:
            prefix_clauses = ["UPPER(p.used_in_course) LIKE %s" for _ in custom_prefixes]
            or_pfx = " OR ".join(prefix_clauses)
            params = [f"%{pfx}%" for pfx in custom_prefixes]
            cur.execute(f"""
                SELECT p.product_id, p.product_name, p.selling_price, p.used_in_course 
                FROM product p
                WHERE p.sold_date IS NULL AND ({or_pfx}) AND p.student_id != %s
                ORDER BY p.product_id DESC
                LIMIT 6
            """, tuple(params + [student_id]))
            for row in (cur.fetchall() or []):
                if extract_course_prefixes(row.get('used_in_course')) & custom_prefixes:
                    if not any(m['product_id'] == row['product_id'] for m in matching_listings):
                        matching_listings.append(row)

            # Fallback for single-user testing
            if not matching_listings:
                cur.execute(f"""
                    SELECT p.product_id, p.product_name, p.selling_price, p.used_in_course 
                    FROM product p
                    WHERE p.sold_date IS NULL AND ({or_pfx})
                    ORDER BY p.product_id DESC
                    LIMIT 6
                """, tuple(params))
                for row in (cur.fetchall() or []):
                    if extract_course_prefixes(row.get('used_in_course')) & custom_prefixes:
                        if not any(m['product_id'] == row['product_id'] for m in matching_listings):
                            matching_listings.append(row)
        except Exception as e:
            print(f"Error matching on add-custom: {e}")

    mysql.connection.commit()
    cur.close()

    new_count = get_wishlist_count(student_id)

    match_msg = f"Added '{item_name}' to your wishlist requests!"
    if matching_listings:
        pfx_label = ", ".join(sorted(custom_prefixes))
        match_msg += f" We found {len(matching_listings)} matching {pfx_label} product(s) already in the marketplace!"

    if is_ajax:
        return jsonify({
            'success': True,
            'message': match_msg,
            'wishlist_count': new_count,
            'match_count': len(matching_listings)
        })

    return redirect(url_for('wishlist_view'))


@app.route('/wishlist/remove-custom/<int:item_id>', methods=['GET', 'POST'])
def remove_custom_wishlist_item(item_id):
    student_id = session.get('student_id')
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json
    if not student_id:
        if is_ajax:
            return jsonify({'success': False, 'message': 'Please login first.'}), 401
        return redirect(url_for('login'))

    ensure_db_schema()

    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM wishlist_custom_item WHERE item_id = %s AND student_id = %s", (item_id, student_id))
    mysql.connection.commit()
    cur.close()

    new_count = get_wishlist_count(student_id)

    if is_ajax:
        return jsonify({
            'success': True,
            'message': 'Custom request removed from wishlist.',
            'wishlist_count': new_count
        })

    return redirect(url_for('wishlist_view'))


@app.route('/wishlist/add/<int:product_id>', methods=['GET', 'POST'])
def add_to_wishlist(product_id):
    student_id = session.get('student_id')
    if not student_id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'message': 'Please login first', 'redirect': url_for('login')}), 401
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()

    # Get product info
    cur.execute("SELECT product_id, product_name, category, used_in_course, selling_price FROM product WHERE product_id = %s", (product_id,))
    product = cur.fetchone()
    if not product:
        cur.close()
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'message': 'Product not found'}), 404
        return redirect(url_for('marketplace'))

    # Get or create wishlist
    cur.execute("SELECT wishlist_id FROM wishlist WHERE student_id = %s LIMIT 1", (student_id,))
    w_row = cur.fetchone()
    if not w_row:
        cur.execute("INSERT INTO wishlist (student_id, recommendation) VALUES (%s, %s)", (student_id, ''))
        wishlist_id = cur.lastrowid
    else:
        wishlist_id = w_row['wishlist_id']

    # Insert into includes if not already present
    cur.execute("SELECT * FROM includes WHERE wishlist_id = %s AND product_id = %s", (wishlist_id, product_id))
    already_included = cur.fetchone()
    if not already_included:
        cur.execute("INSERT INTO includes (wishlist_id, product_id) VALUES (%s, %s)", (wishlist_id, product_id))

    # Fetch course-based recommendations for instant feedback matching 1st 3 letters of course code
    course = product.get('used_in_course', '')
    course_recommendations = []
    course_prefixes = extract_course_prefixes(course)

    if course_prefixes:
        pfx_clauses = ["UPPER(p.used_in_course) LIKE %s" for _ in course_prefixes]
        where_pfx = " OR ".join(pfx_clauses)
        params = [f"%{pfx}%" for pfx in course_prefixes]

        cur.execute(f"""
            SELECT p.product_id, p.product_name, p.category, p.selling_price, p.used_in_course,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo
            FROM product p
            WHERE p.sold_date IS NULL
              AND p.student_id != %s
              AND p.product_id != %s
              AND ({where_pfx})
            ORDER BY p.product_id DESC
            LIMIT 8
        """, tuple([student_id, product_id] + params))
        rows = list(cur.fetchall() or [])

        # Fallback for single-user testing
        if not rows:
            cur.execute(f"""
                SELECT p.product_id, p.product_name, p.category, p.selling_price, p.used_in_course,
                       (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo
                FROM product p
                WHERE p.sold_date IS NULL
                  AND p.product_id != %s
                  AND ({where_pfx})
                ORDER BY p.product_id DESC
                LIMIT 8
            """, tuple([product_id] + params))
            rows = list(cur.fetchall() or [])

        for r in rows:
            if extract_course_prefixes(r.get('used_in_course')) & course_prefixes:
                course_recommendations.append(r)

        pfx_str = ", ".join(sorted(course_prefixes))
        rec_text = f"Smart recommendations for {pfx_str}: " + ", ".join([r['product_name'] for r in course_recommendations[:4]]) if course_recommendations else f"Added {product['product_name']} for {pfx_str}"
        cur.execute("UPDATE wishlist SET recommendation = %s WHERE wishlist_id = %s", (rec_text, wishlist_id))

    mysql.connection.commit()
    cur.close()

    pfx_list = sorted(list(course_prefixes))
    pfx_label = pfx_list[0] if pfx_list else ''
    match_msg = f"Added '{product['product_name']}' to your wishlist!"
    if course_recommendations:
        match_msg += f" Found {len(course_recommendations)} recommended items matching course prefix '{pfx_label}' in marketplace."

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({
            'success': True,
            'message': match_msg,
            'already_in': bool(already_included),
            'wishlist_count': get_wishlist_count(student_id),
            'course': course,
            'course_prefix': pfx_label,
            'matched_count': len(course_recommendations),
            'recommendations': course_recommendations
        })

    return redirect(url_for('wishlist_view'))


@app.route('/wishlist/remove/<int:product_id>', methods=['GET', 'POST'])
def remove_from_wishlist(product_id):
    student_id = session.get('student_id')
    if not student_id:
        if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
            return jsonify({'success': False, 'message': 'Please login first'}), 401
        return redirect(url_for('login'))

    cur = mysql.connection.cursor()
    cur.execute("SELECT wishlist_id FROM wishlist WHERE student_id = %s LIMIT 1", (student_id,))
    w_row = cur.fetchone()
    if w_row:
        cur.execute("DELETE FROM includes WHERE wishlist_id = %s AND product_id = %s", (w_row['wishlist_id'], product_id))
        mysql.connection.commit()
    cur.close()

    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' or request.is_json:
        return jsonify({
            'success': True,
            'message': 'Item removed from wishlist',
            'wishlist_count': get_wishlist_count(student_id)
        })

    return redirect(url_for('wishlist_view'))


@app.route('/api/wishlist/recommendations/<int:product_id>')
def api_wishlist_recommendations(product_id):
    student_id = session.get('student_id')
    cur = mysql.connection.cursor()
    cur.execute("SELECT product_id, product_name, category, used_in_course FROM product WHERE product_id = %s", (product_id,))
    product = cur.fetchone()
    if not product:
        cur.close()
        return jsonify({'success': False, 'recommendations': []})

    course = product.get('used_in_course', '')
    recs = []
    pfxs = extract_course_prefixes(course)
    if pfxs:
        pfx_clauses = ["UPPER(p.used_in_course) LIKE %s" for _ in pfxs]
        where_pfx = " OR ".join(pfx_clauses)
        params = [f"%{pfx}%" for pfx in pfxs]
        cur.execute(f"""
            SELECT p.product_id, p.product_name, p.category, p.selling_price, p.used_in_course,
                   u.name AS seller_name,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) AS photo
            FROM product p
            JOIN user u ON p.student_id = u.student_id
            WHERE p.sold_date IS NULL
              AND p.product_id != %s
              AND ({where_pfx})
            ORDER BY p.product_id DESC
            LIMIT 6
        """, tuple([product_id] + params))
        for r in (cur.fetchall() or []):
            if extract_course_prefixes(r.get('used_in_course')) & pfxs:
                recs.append(r)

    cur.close()
    return jsonify({
        'success': True,
        'course': course,
        'course_prefixes': list(pfxs),
        'recommendations': recs
    })


@app.route('/api/notifications/read/<int:n_id>', methods=['POST'])
def mark_notification_read(n_id):
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False}), 401

    cur = mysql.connection.cursor()
    cur.execute("UPDATE notification SET notification_status = 'read' WHERE n_id = %s AND buyer_notification = %s", (n_id, student_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'success': True, 'unread_count': get_unread_notifications_count(student_id)})


@app.route('/api/notifications/read-all', methods=['POST'])
def mark_all_notifications_read():
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False}), 401

    cur = mysql.connection.cursor()
    cur.execute("UPDATE notification SET notification_status = 'read' WHERE buyer_notification = %s", (student_id,))
    mysql.connection.commit()
    cur.close()
    return jsonify({'success': True, 'unread_count': 0})


@app.route('/api/notifications/latest')
def get_latest_notifications():
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False, 'notifications': [], 'unread_count': 0})

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT n.n_id, n.text, n.notification_type, n.notification_status,
               n.notification_date, n.user_notification AS sender_id,
               u.name AS sender_name
        FROM notification n
        LEFT JOIN user u ON n.user_notification = u.student_id
        WHERE n.buyer_notification = %s
        ORDER BY n.notification_date DESC
        LIMIT 10
    """, (student_id,))
    notifs = cur.fetchall() or []

    formatted_notifs = []
    for notif in notifs:
        dt = notif.get('notification_date')
        time_str = format_message_time(dt) if dt else 'Recently'
        formatted_notifs.append({
            'n_id': notif['n_id'],
            'text': notif['text'],
            'type': notif['notification_type'],
            'status': notif['notification_status'],
            'time': time_str,
            'sender_name': notif.get('sender_name') or 'Peer'
        })

    cur.close()
    unread_count = get_unread_notifications_count(student_id)
    return jsonify({
        'success': True,
        'notifications': formatted_notifs,
        'unread_count': unread_count
    })


@app.route('/notifications')
def notifications_view():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    filter_type = request.args.get('type', 'all').strip()

    cur = mysql.connection.cursor()
    query = """
        SELECT n.n_id, n.buyer_notification, n.user_notification, n.wishlist_id,
               n.text, n.notification_type, n.notification_date, n.notification_status,
               u.name AS sender_name
        FROM notification n
        LEFT JOIN user u ON n.user_notification = u.student_id
        WHERE n.buyer_notification = %s
    """
    params = [student_id]
    if filter_type == 'unread':
        query += " AND n.notification_status = 'unread'"
    elif filter_type == 'wishlist':
        query += " AND n.notification_type = 'wishlist_match'"
    elif filter_type == 'orders':
        query += " AND n.notification_type IN ('order_confirmed', 'payment_received')"
    elif filter_type == 'chat':
        query += " AND n.notification_type = 'chat'"

    query += " ORDER BY n.notification_date DESC, n.n_id DESC LIMIT 50"
    cur.execute(query, tuple(params))
    notifications = cur.fetchall() or []

    for n in notifications:
        n['formatted_time'] = format_message_time(n.get('notification_date'))

    # Overall notification counts for tabs and statistics
    cur.execute("""
        SELECT 
            COUNT(*) as total_count,
            SUM(CASE WHEN notification_status = 'unread' THEN 1 ELSE 0 END) as unread_count,
            SUM(CASE WHEN notification_type = 'wishlist_match' THEN 1 ELSE 0 END) as wishlist_count,
            SUM(CASE WHEN notification_type IN ('order_confirmed', 'payment_received') THEN 1 ELSE 0 END) as order_count,
            SUM(CASE WHEN notification_type = 'chat' THEN 1 ELSE 0 END) as chat_count
        FROM notification
        WHERE buyer_notification = %s
    """, (student_id,))
    stats_row = cur.fetchone() or {}

    total_count = stats_row.get('total_count') or 0
    unread_count = stats_row.get('unread_count') or 0
    wishlist_notif_count = stats_row.get('wishlist_count') or 0
    order_notif_count = stats_row.get('order_count') or 0
    chat_notif_count = stats_row.get('chat_count') or 0

    cur.close()

    cart_count = get_cart_count(student_id)
    wishlist_count = get_wishlist_count(student_id)

    return render_template(
        'notifications.html',
        notifications=notifications,
        filter_type=filter_type,
        total_count=total_count,
        unread_count=unread_count,
        wishlist_notif_count=wishlist_notif_count,
        order_notif_count=order_notif_count,
        chat_notif_count=chat_notif_count,
        cart_count=cart_count,
        wishlist_count=wishlist_count
    )


@app.route('/api/notifications/delete/<int:n_id>', methods=['POST'])
def delete_notification(n_id):
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False}), 401

    cur = mysql.connection.cursor()
    cur.execute("DELETE FROM notification WHERE n_id = %s AND buyer_notification = %s", (n_id, student_id))
    mysql.connection.commit()
    cur.close()
    return jsonify({'success': True, 'unread_count': get_unread_notifications_count(student_id)})



# =========================================================================
# CHAT APPLICATION ROUTES (CHAT & PARTICIPATE ENTITIES)
# =========================================================================

@app.route('/chat')
@app.route('/messages')
def chat_view():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    target_peer_id = request.args.get('user', '').strip()
    product_id_arg = request.args.get('product_id', '').strip()

    cur = mysql.connection.cursor()

    # 1. Fetch conversations list for the current student
    cur.execute("""
        SELECT DISTINCT 
            u.student_id, u.name, u.department, u.semester, u.trust_score,
            (
                SELECT c.text 
                FROM participate p1 
                JOIN participate p2 ON p1.chat_id = p2.chat_id 
                JOIN chat c ON c.chat_id = p1.chat_id 
                WHERE p1.student_id = %s AND p2.student_id = u.student_id 
                ORDER BY c.sent_at DESC, c.chat_id DESC 
                LIMIT 1
            ) AS last_message,
            (
                SELECT c.sent_at 
                FROM participate p1 
                JOIN participate p2 ON p1.chat_id = p2.chat_id 
                JOIN chat c ON c.chat_id = p1.chat_id 
                WHERE p1.student_id = %s AND p2.student_id = u.student_id 
                ORDER BY c.sent_at DESC, c.chat_id DESC 
                LIMIT 1
            ) AS last_message_time,
            (
                SELECT c.sender_id 
                FROM participate p1 
                JOIN participate p2 ON p1.chat_id = p2.chat_id 
                JOIN chat c ON c.chat_id = p1.chat_id 
                WHERE p1.student_id = %s AND p2.student_id = u.student_id 
                ORDER BY c.sent_at DESC, c.chat_id DESC 
                LIMIT 1
            ) AS last_sender_id
        FROM user u
        WHERE u.student_id IN (
            SELECT DISTINCT p2.student_id
            FROM participate p1
            JOIN participate p2 ON p1.chat_id = p2.chat_id
            WHERE p1.student_id = %s AND p2.student_id != %s
        )
        ORDER BY last_message_time DESC
    """, (student_id, student_id, student_id, student_id, student_id))
    conversations = cur.fetchall() or []

    for c in conversations:
        c['formatted_time'] = format_message_time(c.get('last_message_time'))

    # If no peer is specified, pick the most recent conversation
    active_peer_id = target_peer_id
    if not active_peer_id and conversations:
        active_peer_id = conversations[0]['student_id']

    # 2. Fetch active peer info
    active_peer = None
    active_peer_trust_score = 4.5
    if active_peer_id:
        cur.execute("SELECT student_id, name, department, semester, trust_score FROM user WHERE student_id = %s", (active_peer_id,))
        active_peer = cur.fetchone()

        if active_peer:
            cur.execute("SELECT AVG(rating) as avg_rating, COUNT(r_id) as total_reviews FROM review WHERE reviewee_id = %s", (active_peer_id,))
            t_data = cur.fetchone()
            if t_data and t_data['total_reviews'] and t_data['total_reviews'] > 0:
                active_peer_trust_score = float(t_data['avg_rating'])
            elif active_peer.get('trust_score'):
                active_peer_trust_score = float(active_peer['trust_score'])

    # 3. Fetch message history between current user and active peer
    messages = []
    if active_peer:
        cur.execute("""
            SELECT c.chat_id, c.text, c.photo, c.sender_id, c.sent_at,
                   u.name as sender_name
            FROM participate p1
            JOIN participate p2 ON p1.chat_id = p2.chat_id
            JOIN chat c ON c.chat_id = p1.chat_id
            LEFT JOIN user u ON c.sender_id = u.student_id
            WHERE p1.student_id = %s AND p2.student_id = %s
            ORDER BY c.sent_at ASC, c.chat_id ASC
        """, (student_id, active_peer['student_id']))
        messages = cur.fetchall() or []

        for m in messages:
            m['formatted_time'] = format_message_time(m.get('sent_at'))

    # 4. Optional Contextual Product Info
    product_context = None
    if product_id_arg and product_id_arg.isdigit():
        cur.execute("""
            SELECT p.product_id, p.product_name, p.selling_price, p.category,
                   (SELECT photo FROM product_photo pp WHERE pp.product_id = p.product_id LIMIT 1) as photo
            FROM product p
            WHERE p.product_id = %s
        """, (int(product_id_arg),))
        product_context = cur.fetchone()

    # 5. Fetch all BRACU students for the "+ New Chat" picker
    cur.execute("""
        SELECT student_id, name, department, semester, trust_score
        FROM user
        WHERE student_id != %s
        ORDER BY name ASC
    """, (student_id,))
    all_students = cur.fetchall() or []

    cur.close()
    cart_count = get_cart_count(student_id)

    return render_template(
        'chat.html',
        conversations=conversations,
        active_peer=active_peer,
        active_peer_trust_score=active_peer_trust_score,
        messages=messages,
        product_context=product_context,
        all_students=all_students,
        cart_count=cart_count
    )


@app.route('/api/chat/messages/<peer_id>')
def api_chat_messages(peer_id):
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    cur = mysql.connection.cursor()
    cur.execute("""
        SELECT c.chat_id, c.text, c.photo, c.sender_id, c.sent_at,
               u.name as sender_name
        FROM participate p1
        JOIN participate p2 ON p1.chat_id = p2.chat_id
        JOIN chat c ON c.chat_id = p1.chat_id
        LEFT JOIN user u ON c.sender_id = u.student_id
        WHERE p1.student_id = %s AND p2.student_id = %s
        ORDER BY c.sent_at ASC, c.chat_id ASC
    """, (student_id, peer_id))
    messages = cur.fetchall() or []
    cur.close()

    result = []
    for m in messages:
        result.append({
            'chat_id': m['chat_id'],
            'text': m['text'],
            'photo': m['photo'],
            'sender_id': m['sender_id'],
            'sender_name': m['sender_name'],
            'sent_at': str(m['sent_at']),
            'formatted_time': format_message_time(m.get('sent_at')),
            'is_me': (m['sender_id'] == student_id)
        })

    return jsonify({'success': True, 'messages': result})


@app.route('/api/chat/send', methods=['POST'])
def api_chat_send():
    student_id = session.get('student_id')
    if not student_id:
        return jsonify({'success': False, 'error': 'Unauthorized'}), 401

    data = request.get_json(silent=True) or request.form
    receiver_id = data.get('receiver_id', '').strip()
    text = data.get('text', '').strip()
    photo = data.get('photo', '').strip() or None

    if not receiver_id:
        return jsonify({'success': False, 'error': 'Receiver student ID is required'}), 400

    if not text and not photo:
        return jsonify({'success': False, 'error': 'Message content cannot be empty'}), 400

    cur = mysql.connection.cursor()

    # Verify receiver exists
    cur.execute("SELECT student_id, name FROM user WHERE student_id = %s", (receiver_id,))
    receiver = cur.fetchone()
    if not receiver:
        cur.close()
        return jsonify({'success': False, 'error': 'Recipient not found'}), 404

    # 1. Insert into CHAT entity
    cur.execute("""
        INSERT INTO chat (text, photo, sender_id, sent_at)
        VALUES (%s, %s, %s, NOW())
    """, (text, photo, student_id))
    new_chat_id = cur.lastrowid

    # 2. Insert into PARTICIPATE entity (both sender and receiver)
    try:
        cur.execute("INSERT IGNORE INTO participate (student_id, chat_id) VALUES (%s, %s)", (student_id, new_chat_id))
        cur.execute("INSERT IGNORE INTO participate (student_id, chat_id) VALUES (%s, %s)", (receiver_id, new_chat_id))
    except Exception as e:
        pass

    # 3. Create a peer notification
    try:
        sender_name = session.get('user_name') or 'A student'
        msg_preview = f'"{text[:40]}..."' if (text and len(text) > 40) else (f'"{text}"' if text else 'Sent an attachment photo')
        notif_text = f"💬 {sender_name} texted you: {msg_preview}"
        cur.execute("""
            INSERT INTO notification 
            (buyer_notification, user_notification, text, notification_type, notification_status)
            VALUES (%s, %s, %s, 'chat', 'unread')
        """, (receiver_id, student_id, notif_text))
    except Exception as e:
        print("Error creating chat notification:", e)

    mysql.connection.commit()
    cur.close()

    now_dt = datetime.now()
    return jsonify({
        'success': True,
        'chat': {
            'chat_id': new_chat_id,
            'text': text,
            'photo': photo,
            'sender_id': student_id,
            'sent_at': str(now_dt),
            'formatted_time': format_message_time(now_dt)
        }
    })


@app.route('/chat/send', methods=['POST'])
def form_chat_send():
    student_id = session.get('student_id')
    if not student_id:
        return redirect(url_for('login'))

    receiver_id = request.form.get('receiver_id', '').strip()
    text = request.form.get('text', '').strip()
    photo = request.form.get('photo', '').strip() or None

    if receiver_id and (text or photo):
        cur = mysql.connection.cursor()
        cur.execute("""
            INSERT INTO chat (text, photo, sender_id, sent_at)
            VALUES (%s, %s, %s, NOW())
        """, (text, photo, student_id))
        new_chat_id = cur.lastrowid

        try:
            cur.execute("INSERT IGNORE INTO participate (student_id, chat_id) VALUES (%s, %s)", (student_id, new_chat_id))
            cur.execute("INSERT IGNORE INTO participate (student_id, chat_id) VALUES (%s, %s)", (receiver_id, new_chat_id))
        except Exception:
            pass

        try:
            sender_name = session.get('user_name') or 'A student'
            msg_preview = f'"{text[:40]}..."' if (text and len(text) > 40) else (f'"{text}"' if text else 'Sent an attachment photo')
            notif_text = f"💬 {sender_name} texted you: {msg_preview}"
            cur.execute("""
                INSERT INTO notification 
                (buyer_notification, user_notification, text, notification_type, notification_status)
                VALUES (%s, %s, %s, 'chat', 'unread')
            """, (receiver_id, student_id, notif_text))
        except Exception:
            pass

        mysql.connection.commit()
        cur.close()

    return redirect(url_for('chat_view', user=receiver_id))


@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))


@app.route('/reset-password', methods=['POST'])
def reset_password():
    identifier = request.form.get('reset_identifier', '').strip()
    new_password = request.form.get('new_password', '')
    confirm_password = request.form.get('confirm_password', '')

    if not identifier:
        return render_template('login.html', reset_error="Please enter your Email or Student ID.")

    if not new_password or not confirm_password:
        return render_template('login.html', reset_error="Please fill in both password fields.")

    if new_password != confirm_password:
        return render_template('login.html', reset_error="Passwords do not match. Please try again.")

    if len(new_password) < 4:
        return render_template('login.html', reset_error="Password must be at least 4 characters long.")

    cur = mysql.connection.cursor()
    cur.execute(
        "SELECT * FROM user WHERE gsuite_email = %s OR student_id = %s",
        (identifier, identifier)
    )
    user = cur.fetchone()

    if not user:
        cur.close()
        return render_template('login.html', reset_error="No account found with this Email or Student ID.")

    new_hash = generate_password_hash(new_password)
    cur.execute(
        "UPDATE user SET password_hash = %s WHERE student_id = %s",
        (new_hash, user['student_id'])
    )
    mysql.connection.commit()
    cur.close()

    return render_template(
        'login.html',
        success="Password reset successfully! You can now login with your new password.",
        identifier=identifier
    )


if __name__ == '__main__':
    app.run(debug=True)
