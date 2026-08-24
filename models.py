from datetime import datetime, timedelta
from werkzeug.security import generate_password_hash, check_password_hash
import secrets

from extensions import db
from flask_login import UserMixin


class User(UserMixin, db.Model):
    __tablename__ = 'users'
    id = db.Column(db.String, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(256), nullable=False)
    first_name = db.Column(db.String(50), nullable=True)
    last_name = db.Column(db.String(50), nullable=True)
    profile_image_url = db.Column(db.String, nullable=True)
    active = db.Column(db.Boolean, default=True)

    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    # Relationship to AI prompts
    ai_prompts = db.relationship('UserAIPrompt', backref='user', lazy=True, cascade='all, delete-orphan')
    
    # Relationship to instruction presets
    instruction_presets = db.relationship('InstructionPreset', backref='user', lazy=True, cascade='all, delete-orphan')

    def set_password(self, password):
        """Hash and set the user's password."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        """Check if the provided password matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    def __repr__(self):
        return f'<User {self.username}>'


# Connected Shopify stores (multi-tenant: each user connects their own store via OAuth)
class Shop(db.Model):
    __tablename__ = 'shops'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    shop_domain = db.Column(db.String(255), unique=True, nullable=False)  # e.g. "my-store.myshopify.com"
    access_token = db.Column(db.String(512), nullable=False)
    scope = db.Column(db.String(512), nullable=True)  # OAuth scopes granted
    shop_name = db.Column(db.String(255), nullable=True)  # Friendly display name
    installed_at = db.Column(db.DateTime, default=datetime.now)
    is_active = db.Column(db.Boolean, default=True)

    user = db.relationship('User', backref='shops')

    def __repr__(self):
        return f'<Shop {self.shop_domain}>'


# User AI prompt presets
class UserAIPrompt(db.Model):
    __tablename__ = 'user_ai_prompts'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    workflow_type = db.Column(db.String, nullable=False)  # 'mockup' or 'pre-framed'
    prompt_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    __table_args__ = (db.UniqueConstraint('user_id', 'workflow_type', name='uq_user_workflow_prompt'),)


# User variant presets
class UserVariantPreset(db.Model):
    __tablename__ = 'user_variant_presets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    preset_name = db.Column(db.String, nullable=False)
    variants_data = db.Column(db.Text, nullable=False)  # JSON string
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship('User', backref='variant_presets')


# User configuration profiles (unified preset system)
class UserProfile(db.Model):
    __tablename__ = 'user_profiles'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    profile_name = db.Column(db.String(100), nullable=False)
    
    # Profile type: 'ready' for Ready Images workflow, 'csv' for Bulk CSV Import workflow
    profile_type = db.Column(db.String(20), nullable=False, default='ready')
    
    # All configuration data stored as JSON
    custom_prompt = db.Column(db.Text, nullable=True)
    custom_prompt_sections = db.Column(db.Text, nullable=True)  # JSON array of {id, content} for lossless prompt roundtrip
    product_vendor = db.Column(db.String(100), nullable=True)
    product_type = db.Column(db.String(100), nullable=True)
    listing_status = db.Column(db.String(20), nullable=True)
    review_before_publish = db.Column(db.Boolean, default=False)
    inventory_quantity = db.Column(db.String(10), nullable=True)
    inventory_policy = db.Column(db.String(20), nullable=True)
    
    # Manual toggles
    title_manual = db.Column(db.Boolean, default=False)
    description_manual = db.Column(db.Boolean, default=False)
    category_manual = db.Column(db.Boolean, default=False)
    product_type_manual = db.Column(db.Boolean, default=False)
    tags_manual = db.Column(db.Boolean, default=False)
    collections_manual = db.Column(db.Boolean, default=False)
    collections_enabled = db.Column(db.Boolean, default=True)  # When True, AI picks from store collections; when False, no collections
    vendor_manual = db.Column(db.Boolean, default=False)
    sku_manual = db.Column(db.Boolean, default=False)
    handle_manual = db.Column(db.Boolean, default=False)
    color_manual = db.Column(db.Boolean, default=False)
    frame_style_manual = db.Column(db.Boolean, default=False)
    theme_manual = db.Column(db.Boolean, default=False)
    condition_manual = db.Column(db.Boolean, default=False)
    decoration_material_manual = db.Column(db.Boolean, default=False)
    artwork_frame_material_manual = db.Column(db.Boolean, default=False)
    subject_manual = db.Column(db.Boolean, default=False)
    room_manual = db.Column(db.Boolean, default=False)
    mood_manual = db.Column(db.Boolean, default=False)
    palette_manual = db.Column(db.Boolean, default=False)
    audience_manual = db.Column(db.Boolean, default=False)
    occasion_manual = db.Column(db.Boolean, default=False)
    season_manual = db.Column(db.Boolean, default=False)
    composition_manual = db.Column(db.Boolean, default=False)
    display_suggestion_manual = db.Column(db.Boolean, default=False)
    material_manual = db.Column(db.Boolean, default=False)
    art_movement_manual = db.Column(db.Boolean, default=False)
    art_style_manual = db.Column(db.Boolean, default=False)
    artwork_authenticity_manual = db.Column(db.Boolean, default=False)
    orientation_manual = db.Column(db.Boolean, default=False)
    seo_title_manual = db.Column(db.Boolean, default=False)
    meta_desc_manual = db.Column(db.Boolean, default=False)
    google_shopping_enabled = db.Column(db.Boolean, default=True)
    google_category_manual = db.Column(db.Boolean, default=False)
    gender_manual = db.Column(db.Boolean, default=False)
    age_group_manual = db.Column(db.Boolean, default=False)
    gs_condition_manual = db.Column(db.Boolean, default=False)
    custom_product_manual = db.Column(db.Boolean, default=False)
    custom_label_0_manual = db.Column(db.Boolean, default=False)
    custom_label_1_manual = db.Column(db.Boolean, default=False)
    custom_label_2_manual = db.Column(db.Boolean, default=False)
    custom_label_3_manual = db.Column(db.Boolean, default=False)
    custom_label_4_manual = db.Column(db.Boolean, default=False)
    
    # Manual values
    manual_title = db.Column(db.Text, nullable=True)
    manual_description = db.Column(db.Text, nullable=True)
    manual_category = db.Column(db.String(100), nullable=True)
    manual_category_gid = db.Column(db.String(200), nullable=True)
    manual_tags = db.Column(db.Text, nullable=True)
    manual_collections = db.Column(db.Text, nullable=True)
    manual_sku = db.Column(db.String(100), nullable=True)
    manual_handle = db.Column(db.String(100), nullable=True)
    manual_color = db.Column(db.String(50), nullable=True)
    manual_frame_style = db.Column(db.String(50), nullable=True)
    manual_theme = db.Column(db.String(50), nullable=True)
    manual_condition = db.Column(db.String(100), nullable=True)
    manual_decoration_material = db.Column(db.String(100), nullable=True)
    manual_artwork_frame_material = db.Column(db.String(100), nullable=True)
    manual_subject = db.Column(db.String(150), nullable=True)
    manual_room = db.Column(db.String(150), nullable=True)
    manual_mood = db.Column(db.String(150), nullable=True)
    manual_palette = db.Column(db.String(150), nullable=True)
    manual_audience = db.Column(db.String(150), nullable=True)
    manual_occasion = db.Column(db.String(150), nullable=True)
    manual_season = db.Column(db.String(150), nullable=True)
    manual_composition = db.Column(db.String(255), nullable=True)
    manual_display_suggestion = db.Column(db.Text, nullable=True)
    manual_material = db.Column(db.String(150), nullable=True)
    manual_art_movement = db.Column(db.String(150), nullable=True)
    manual_art_style = db.Column(db.String(150), nullable=True)
    manual_artwork_authenticity = db.Column(db.String(150), nullable=True)
    manual_orientation = db.Column(db.String(100), nullable=True)
    manual_seo_title = db.Column(db.String(255), nullable=True)
    manual_meta_desc = db.Column(db.Text, nullable=True)
    manual_google_product_category = db.Column(db.String(255), nullable=True)
    manual_gender = db.Column(db.String(50), nullable=True)
    manual_age_group = db.Column(db.String(50), nullable=True)
    manual_gs_condition = db.Column(db.String(50), nullable=True)
    manual_custom_product = db.Column(db.String(20), nullable=True)
    manual_custom_label_0 = db.Column(db.String(100), nullable=True)
    manual_custom_label_1 = db.Column(db.String(100), nullable=True)
    manual_custom_label_2 = db.Column(db.String(100), nullable=True)
    manual_custom_label_3 = db.Column(db.String(100), nullable=True)
    manual_custom_label_4 = db.Column(db.String(100), nullable=True)
    
    # Variants data
    variants_data = db.Column(db.Text, nullable=True)  # JSON string
    
    # Publishing channels and catalogs (Shopify's official terms)
    selected_channels = db.Column(db.Text, nullable=True)  # JSON string - publication IDs
    selected_markets = db.Column(db.Text, nullable=True)   # JSON string - market/catalog IDs
    
    # CSV-specific fields (only used when profile_type='csv')
    csv_custom_prompt = db.Column(db.Text, nullable=True)  # Bulk CSV custom AI prompt
    csv_field_settings = db.Column(db.Text, nullable=True)  # JSON string - field mode/value for CSV fields
    csv_use_main_image_per_variant = db.Column(db.Boolean, default=False)
    
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship('User', backref='profiles')
    
    __table_args__ = (db.UniqueConstraint('user_id', 'profile_name', 'profile_type', name='uq_user_profile_name_type'),)


# User AI instruction presets
class UserAIInstructionPreset(db.Model):
    __tablename__ = 'user_ai_instruction_presets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    preset_name = db.Column(db.String, nullable=False)
    instructions = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship('User', backref='ai_instruction_presets')


# User collections presets
class UserCollectionPreset(db.Model):
    __tablename__ = 'user_collection_presets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    collection_name = db.Column(db.String(100), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    
    user = db.relationship('User', backref='collection_presets')

# User product organization presets
class UserProductPreset(db.Model):
    __tablename__ = 'user_product_presets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    business_name = db.Column(db.String(255), nullable=True)
    product_type = db.Column(db.String(255), nullable=True)
    vendor = db.Column(db.String(255), nullable=True)
    platform = db.Column(db.String(100), nullable=True, default='Shopify')
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    user = db.relationship('User', backref='product_presets')


class UserFrameTemplate(db.Model):
    __tablename__ = 'user_frame_templates'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    display_name = db.Column(db.String(255), nullable=False)
    original_filename = db.Column(db.String(255), nullable=False)
    stored_filename = db.Column(db.String(255), nullable=False)
    file_path = db.Column(db.Text, nullable=False)
    smart_layer_name = db.Column(db.String(100), nullable=False, default='1')
    sort_order = db.Column(db.Integer, nullable=False, default=0)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)

    user = db.relationship('User', backref='frame_templates')


# User instruction presets for Custom AI Instructions
class InstructionPreset(db.Model):
    __tablename__ = 'instruction_presets'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    preset_name = db.Column(db.String(100), nullable=False)
    instruction_content = db.Column(db.Text, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.now)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now)
    
    __table_args__ = (db.UniqueConstraint('user_id', 'preset_name', name='uq_user_preset_name'),)


# Password reset tokens (for "Forgot password")
class PasswordResetToken(db.Model):
    __tablename__ = 'password_reset_tokens'
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)

    user = db.relationship('User', backref='password_reset_tokens')

    @staticmethod
    def create_for_user(user, expires_in_hours=1):
        """Create a new reset token for the user. Invalidates any existing tokens for this user."""
        PasswordResetToken.query.filter_by(user_id=user.id).delete()
        token = PasswordResetToken(
            user_id=user.id,
            token=secrets.token_urlsafe(32),
            expires_at=datetime.utcnow() + timedelta(hours=expires_in_hours)
        )
        db.session.add(token)
        return token


class BulkEditSnapshot(db.Model):
    """Compressed before-state retained for bulk-edit audit and recovery."""

    __tablename__ = 'bulk_edit_snapshots'
    id = db.Column(db.String(36), primary_key=True)
    job_id = db.Column(db.String(36), nullable=True, index=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False, index=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id'), nullable=False, index=True)
    shop_domain = db.Column(db.String(255), nullable=False)
    item_count = db.Column(db.Integer, nullable=False, default=0)
    payload_compressed = db.Column(db.Text, nullable=False)
    status = db.Column(db.String(24), nullable=False, default='saved')
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    expires_at = db.Column(db.DateTime, nullable=False, index=True)

    user = db.relationship('User', backref='bulk_edit_snapshots')
    shop = db.relationship('Shop', backref='bulk_edit_snapshots')


class ShopifyCatalogueCacheState(db.Model):
    """Active durable Shopify catalogue generation for one connected shop."""

    __tablename__ = 'shopify_catalogue_cache_states'
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id'), nullable=False, unique=True, index=True)
    user_id = db.Column(db.String, db.ForeignKey('users.id'), nullable=False, index=True)
    active_generation = db.Column(db.String(36), nullable=False, index=True)
    product_count = db.Column(db.Integer, nullable=False, default=0)
    sync_status = db.Column(db.String(24), nullable=False, default='ready')
    last_error = db.Column(db.Text, nullable=True)
    last_full_sync_at = db.Column(db.DateTime, nullable=True)
    last_incremental_sync_at = db.Column(db.DateTime, nullable=True)
    last_shopify_updated_at = db.Column(db.String(64), nullable=True)
    resources_json = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.now, nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.now, onupdate=datetime.now, nullable=False)

    shop = db.relationship('Shop', backref='catalogue_cache_state', uselist=False)
    user = db.relationship('User', backref='catalogue_cache_states')


class ShopifyCachedProduct(db.Model):
    """Compressed normalized product row belonging to an immutable cache generation."""

    __tablename__ = 'shopify_cached_products'
    id = db.Column(db.Integer, primary_key=True)
    shop_id = db.Column(db.Integer, db.ForeignKey('shops.id'), nullable=False, index=True)
    generation = db.Column(db.String(36), nullable=False, index=True)
    product_gid = db.Column(db.String(255), nullable=False)
    handle = db.Column(db.String(255), nullable=True, index=True)
    shopify_updated_at = db.Column(db.String(64), nullable=True, index=True)
    position = db.Column(db.Integer, nullable=False, default=0)
    payload_compressed = db.Column(db.Text, nullable=False)
    cached_at = db.Column(db.DateTime, default=datetime.now, nullable=False)

    shop = db.relationship('Shop', backref='cached_catalogue_products')

    __table_args__ = (
        db.UniqueConstraint('shop_id', 'generation', 'product_gid', name='uq_shop_catalogue_generation_product'),
        db.Index('ix_shop_catalogue_generation_position', 'shop_id', 'generation', 'position'),
    )
