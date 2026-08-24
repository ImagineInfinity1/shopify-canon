import os
import logging
from datetime import datetime
from functools import wraps
from flask import request, redirect, url_for, session, flash, render_template
from flask_login import LoginManager, login_user, logout_user, current_user, login_required
from werkzeug.security import check_password_hash

from app import app
from extensions import db
from models import User, PasswordResetToken


def is_registration_enabled():
    """Check if public registration is enabled via environment variable."""
    return os.environ.get('REGISTRATION_ENABLED', 'false').lower() in ('true', '1', 'yes')

logger = logging.getLogger(__name__)

# Flask-Login: init_app() must be called from app.py so the same app instance gets login_manager
login_manager = LoginManager()
login_manager.login_view = 'login'  # type: ignore
login_manager.login_message = 'Please log in to access this page.'
login_manager.login_message_category = 'info'


def init_app(app):
    """Attach login_manager to the Flask app. Call this from app.py after creating the app."""
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(user_id)

    @app.context_processor
    def inject_registration_flag():
        """Make registration_enabled available in all templates."""
        return {'registration_enabled': is_registration_enabled()}


def require_login(f):
    """Decorator to require login for routes."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        if not current_user.is_authenticated:
            session["next_url"] = request.url
            return redirect(url_for('login'))
        return f(*args, **kwargs)
    return decorated_function


# Auth routes
@app.route('/login', methods=['GET', 'POST'])
def login():
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username_or_email = request.form.get('username', '').strip()
        password = request.form.get('password', '')
        
        if not username_or_email or not password:
            flash('Please enter both username/email and password.', 'error')
            return render_template('login.html')
        
        # Allow login by username or email
        if '@' in username_or_email:
            user = User.query.filter_by(email=username_or_email.lower()).first()
        else:
            user = User.query.filter_by(username=username_or_email).first()
        
        if user and user.check_password(password):
            login_user(user, remember=True)
            flash(f'Welcome back, {user.username}!', 'success')
            
            # Redirect to next page or dashboard
            next_page = session.pop('next_url', None)
            return redirect(next_page or url_for('index'))
        else:
            flash('Invalid username or password.', 'error')
    
    return render_template('login.html')


@app.route('/register', methods=['GET', 'POST'])
def register():
    if not is_registration_enabled():
        flash('Registration is currently closed. Please contact the administrator for an account.', 'info')
        return redirect(url_for('login'))

    if current_user.is_authenticated:
        return redirect(url_for('index'))
    
    if request.method == 'POST':
        username = request.form.get('username', '').strip()
        email = request.form.get('email', '').strip()
        password = request.form.get('password', '')
        confirm_password = request.form.get('confirm_password', '')
        
        # Validation
        if not all([username, email, password, confirm_password]):
            flash('All fields are required.', 'error')
            return render_template('register.html')
        
        if password != confirm_password:
            flash('Passwords do not match.', 'error')
            return render_template('register.html')
        
        if len(password) < 6:
            flash('Password must be at least 6 characters long.', 'error')
            return render_template('register.html')
        
        # Check if username or email already exists
        if User.query.filter_by(username=username).first():
            flash('Username already exists. Please choose a different one.', 'error')
            return render_template('register.html')
        
        if User.query.filter_by(email=email).first():
            flash('Email already registered. Please use a different email.', 'error')
            return render_template('register.html')
        
        # Create new user
        try:
            # Generate unique ID for the user (using same format as existing data)
            import uuid
            user_id = str(uuid.uuid4())[:8]  # 8 character ID similar to existing format
            
            user = User(id=user_id, username=username, email=email, password_hash='')
            user.set_password(password)
            db.session.add(user)
            db.session.commit()
            
            # Login the new user
            login_user(user, remember=True)
            flash(f'Account created successfully! Welcome, {username}!', 'success')
            return redirect(url_for('index'))
        
        except Exception as e:
            db.session.rollback()
            logger.error(f"Registration error: {str(e)}")
            flash('An error occurred while creating your account. Please try again.', 'error')
            return render_template('register.html')
    
    return render_template('register.html')


@app.route('/account', methods=['GET', 'POST'])
@login_required
def account():
    """Account page: view and edit profile (email, name, password)."""
    if request.method == 'POST':
        action = request.form.get('action', '')
        if action == 'update_profile':
            current_user.first_name = request.form.get('first_name', '').strip() or None
            current_user.last_name = request.form.get('last_name', '').strip() or None
            new_email = request.form.get('email', '').strip().lower()
            if new_email and new_email != current_user.email:
                if User.query.filter_by(email=new_email).first():
                    flash('That email is already in use.', 'error')
                else:
                    current_user.email = new_email
            db.session.commit()
            flash('Profile updated.', 'success')
        elif action == 'change_password':
            current = request.form.get('current_password', '')
            new_pw = request.form.get('new_password', '')
            confirm = request.form.get('confirm_password', '')
            if not current or not current_user.check_password(current):
                flash('Current password is incorrect.', 'error')
            elif not new_pw or len(new_pw) < 6:
                flash('New password must be at least 6 characters.', 'error')
            elif new_pw != confirm:
                flash('New passwords do not match.', 'error')
            else:
                current_user.set_password(new_pw)
                db.session.commit()
                flash('Password updated.', 'success')
        return redirect(url_for('account'))
    # Load connected shops for the account page
    from models import Shop
    user_shops = Shop.query.filter_by(user_id=current_user.id, is_active=True).all()
    return render_template('account.html', shops=user_shops)


@app.route('/logout')
@login_required
def logout():
    logout_user()
    flash('You have been logged out.', 'info')
    return redirect(url_for('landing'))


def _send_reset_email(user, reset_link):
    """Send password reset email if MAIL_* env is configured."""
    import os
    import smtplib
    from email.mime.text import MIMEText
    from email.mime.multipart import MIMEMultipart
    mail_server = os.environ.get('MAIL_SERVER')
    if not mail_server:
        return False
    try:
        msg = MIMEMultipart('alternative')
        msg['Subject'] = 'Listing Cannon – Reset your password'
        msg['From'] = os.environ.get('MAIL_DEFAULT_SENDER', 'noreply@listingcannon.com')
        msg['To'] = user.email
        text = f"Hi {user.username},\n\nReset your password by opening this link (valid for 1 hour):\n{reset_link}\n\nIf you didn't request this, ignore this email.\n"
        msg.attach(MIMEText(text, 'plain'))
        port = int(os.environ.get('MAIL_PORT', '587'))
        use_tls = os.environ.get('MAIL_USE_TLS', 'true').lower() in ('1', 'true', 'yes')
        with smtplib.SMTP(mail_server, port) as s:
            if use_tls:
                s.starttls()
            if os.environ.get('MAIL_USERNAME'):
                s.login(os.environ.get('MAIL_USERNAME'), os.environ.get('MAIL_PASSWORD', ''))
            s.sendmail(msg['From'], [user.email], msg.as_string())
        return True
    except Exception as e:
        logger.warning(f"Failed to send reset email: {e}")
        return False


@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    """Request a password reset link by email."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    if request.method == 'GET':
        return render_template('forgot_password.html')
    email = request.form.get('email', '').strip().lower()
    if not email:
        flash('Please enter your email address.', 'error')
        return render_template('forgot_password.html')
    user = User.query.filter_by(email=email).first()
    if not user:
        # Don't reveal whether the email exists
        flash('If an account exists for that email, you will receive a password reset link.', 'info')
        return redirect(url_for('login'))
    token_record = PasswordResetToken.create_for_user(user, expires_in_hours=1)
    db.session.commit()
    reset_link = request.url_root.rstrip('/') + url_for('reset_password', token=token_record.token)
    email_sent = _send_reset_email(user, reset_link)
    if email_sent:
        flash('If an account exists for that email, you will receive a password reset link shortly.', 'info')
        return redirect(url_for('login'))
    # No mail config: show link on page so user can copy it (development / self-hosted)
    return render_template('forgot_password_sent.html', reset_link=reset_link)


@app.route('/reset_password/<token>', methods=['GET', 'POST'])
def reset_password(token):
    """Set a new password using a valid reset token."""
    if current_user.is_authenticated:
        return redirect(url_for('index'))
    record = PasswordResetToken.query.filter_by(token=token, used=False).first()
    if not record or record.expires_at < datetime.utcnow():
        flash('This reset link is invalid or has expired. Please request a new one.', 'error')
        return redirect(url_for('forgot_password'))
    user = User.query.get(record.user_id)
    if not user:
        flash('Invalid reset link.', 'error')
        return redirect(url_for('forgot_password'))
    if request.method == 'GET':
        return render_template('reset_password.html', token=token)
    password = request.form.get('password', '')
    confirm = request.form.get('confirm_password', '')
    if not password or not confirm:
        flash('Please fill in both password fields.', 'error')
        return render_template('reset_password.html', token=token)
    if password != confirm:
        flash('Passwords do not match.', 'error')
        return render_template('reset_password.html', token=token)
    if len(password) < 6:
        flash('Password must be at least 6 characters.', 'error')
        return render_template('reset_password.html', token=token)
    user.set_password(password)
    record.used = True
    db.session.commit()
    flash('Your password has been reset. You can log in now.', 'success')
    return redirect(url_for('login'))