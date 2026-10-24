from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
from datetime import datetime

app = Flask(__name__)
app.secret_key = '123456789' 
DATABASE = 'database.sqlite'

# Load censorship data
# WARNING! The censorship.dat file contains disturbing language when decrypted. 
# If you want to test whether moderation works, 
# you can trigger censorship using these words: 
# tier1badword, tier2badword, tier3badword
ENCRYPTED_FILE_PATH = 'censorship.dat'
fernet = Fernet('xpplx11wZUibz0E8tV8Z9mf-wwggzSrc21uQ17Qq2gg=')
with open(ENCRYPTED_FILE_PATH, 'rb') as encrypted_file:
    encrypted_data = encrypted_file.read()
decrypted_data = fernet.decrypt(encrypted_data)
MODERATION_CONFIG = json.loads(decrypted_data)
TIER1_WORDS = MODERATION_CONFIG['categories']['tier1_severe_violations']['words']
TIER2_PHRASES = MODERATION_CONFIG['categories']['tier2_spam_scams']['phrases']
TIER3_WORDS = MODERATION_CONFIG['categories']['tier3_mild_profanity']['words']

def get_db():
    """
    Connect to the application's configured database. The connection
    is unique for each request and will be reused if this is called
    again.
    """
    if 'db' not in g:
        g.db = sqlite3.connect(
            DATABASE,
            detect_types=sqlite3.PARSE_DECLTYPES
        )
        g.db.row_factory = sqlite3.Row

    return g.db


@app.teardown_appcontext
def close_connection(exception):
    """Closes the database again at the end of the request."""
    db = g.pop('db', None)

    if db is not None:
        db.close()


def query_db(query, args=(), one=False, commit=False):
    """
    Queries the database and returns a list of dictionaries, a single
    dictionary, or None. Also handles write operations.
    """
    db = get_db()
    
    # Using 'with' on a connection object implicitly handles transactions.
    # The 'with' statement will automatically commit if successful, 
    # or rollback if an exception occurs. This is safer.
    try:
        with db:
            cur = db.execute(query, args)
        
        # For SELECT statements, fetch the results after the transaction block
        if not commit:
            rv = cur.fetchall()
            return (rv[0] if rv else None) if one else rv
        
        # For write operations, we might want the cursor to get info like lastrowid
        return cur

    except sqlite3.Error as e:
        print(f"Database error: {e}")
        return None

@app.template_filter('datetimeformat')
def datetimeformat(value):
    if isinstance(value, datetime):
        dt = value
    elif isinstance(value, str):
        dt = datetime.strptime(value, '%Y-%m-%d %H:%M:%S')
    else:
        return "N/A"
    return dt.strftime('%b %d, %Y %H:%M')

REACTION_EMOJIS = {
    'like': '❤️', 'love': '😍', 'laugh': '😂',
    'wow': '😮', 'sad': '😢', 'angry': '😠',
}
REACTION_TYPES = list(REACTION_EMOJIS.keys())


@app.route('/')
def feed():
    #  1. Get Pagination and Filter Parameters 
    try:
        page = int(request.args.get('page', 1))
    except ValueError:
        page = 1
    sort = request.args.get('sort', 'new').lower()
    show = request.args.get('show', 'all').lower()
    
    # Define how many posts to show per page
    POSTS_PER_PAGE = 10
    offset = (page - 1) * POSTS_PER_PAGE

    current_user_id = session.get('user_id')
    params = []

    #  2. Build the Query 
    where_clause = ""
    if show == 'following' and current_user_id:
        where_clause = "WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)"
        params.append(current_user_id)

    # Add the pagination parameters to the query arguments
    pagination_params = (POSTS_PER_PAGE, offset)

    if sort == 'popular':
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id,
                   IFNULL(r.total_reactions, 0) as total_reactions
            FROM posts p
            JOIN users u ON p.user_id = u.id
            LEFT JOIN (
                SELECT post_id, COUNT(*) as total_reactions FROM reactions GROUP BY post_id
            ) r ON p.id = r.post_id
            {where_clause}
            ORDER BY total_reactions DESC, p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)
    elif sort == 'recommended':
        posts = recommend(current_user_id, show == 'following' and current_user_id)
    else:  # Default sort is 'new'
        query = f"""
            SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
            FROM posts p
            JOIN users u ON p.user_id = u.id
            {where_clause}
            ORDER BY p.created_at DESC
            LIMIT ? OFFSET ?
        """
        final_params = params + list(pagination_params)
        posts = query_db(query, final_params)

    posts_data = []
    for post in posts:
        # Determine if the current user follows the poster
        followed_poster = False
        if current_user_id and post['user_id'] != current_user_id:
            follow_check = query_db(
                'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
                (current_user_id, post['user_id']),
                one=True
            )
            if follow_check:
                followed_poster = True

        # Determine if the current user reacted to this post and with what reaction
        user_reaction = None
        if current_user_id:
            reaction_check = query_db(
                'SELECT reaction_type FROM reactions WHERE user_id = ? AND post_id = ?',
                (current_user_id, post['id']),
                one=True
            )
            if reaction_check:
                user_reaction = reaction_check['reaction_type']

        reactions = query_db('SELECT reaction_type, COUNT(*) as count FROM reactions WHERE post_id = ? GROUP BY reaction_type', (post['id'],))
        comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post['id'],))
        post_dict = dict(post)
        post_dict['content'], _ = moderate_content(post_dict['content'])
        comments_moderated = []
        for comment in comments_raw:
            comment_dict = dict(comment)
            comment_dict['content'], _ = moderate_content(comment_dict['content'])
            comments_moderated.append(comment_dict)
        posts_data.append({
            'post': post_dict,
            'reactions': reactions,
            'user_reaction': user_reaction,
            'followed_poster': followed_poster,
            'comments': comments_moderated
        })

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/posts/new', methods=['POST'])
def add_post():
    """Handles creating a new post from the feed."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to create a post.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content', '')

    # Basic validation to ensure post is not empty
    if not content.strip():
        flash('Post cannot be empty.', 'warning')
        return redirect(url_for('feed'))

    # Design Claim 20: check the draft at the moment of posting.
    # If it breaks a rule, show a reminder page instead of saving straight away.
    reminder_page = check_before_saving(user_id, content, 'post', None,
                                        form_action=url_for('add_post'),
                                        cancel_url=url_for('feed'))
    if reminder_page:
        return reminder_page

    # We save the original text. moderate_content() is applied whenever the post
    # is shown, so the rules (and the admin risk scores) stay in one place.
    db = get_db()
    db.execute('INSERT INTO posts (user_id, content) VALUES (?, ?)',
               (user_id, content))
    db.commit()
    flash('Your post was successfully created!', 'success')

    # Redirect back to the main feed to see the new post
    return redirect(url_for('feed'))
    

@app.route('/posts/<int:post_id>/delete', methods=['POST'])
def delete_post(post_id):
    """Handles deleting a post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a post.', 'danger')
        return redirect(url_for('login'))

    # Find the post in the database
    post = query_db('SELECT id, user_id FROM posts WHERE id = ?', (post_id,), one=True)

    # Check if the post exists and if the current user is the owner
    if not post:
        flash('Post not found.', 'danger')
        return redirect(url_for('feed'))

    if post['user_id'] != user_id:
        # Security check: prevent users from deleting others' posts
        flash('You do not have permission to delete this post.', 'danger')
        return redirect(url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    # To maintain database integrity, delete associated records first
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    # Finally, delete the post itself
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()

    flash('Your post was successfully deleted.', 'success')
    # Redirect back to the page the user came from, or the feed as a fallback
    return redirect(request.referrer or url_for('feed'))

@app.route('/u/<username>')
def user_profile(username):
    """Displays a user's profile page with moderated bio, posts, and latest comments."""
    
    user_raw = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user_raw:
        abort(404)

    user = dict(user_raw)
    moderated_bio, _ = moderate_content(user.get('profile', ''))
    user['profile'] = moderated_bio

    posts_raw = query_db('SELECT id, content, user_id, created_at FROM posts WHERE user_id = ? ORDER BY created_at DESC', (user['id'],))
    posts = []
    for post_raw in posts_raw:
        post = dict(post_raw)
        moderated_post_content, _ = moderate_content(post['content'])
        post['content'] = moderated_post_content
        posts.append(post)

    comments_raw = query_db('SELECT id, content, user_id, post_id, created_at FROM comments WHERE user_id = ? ORDER BY created_at DESC LIMIT 100', (user['id'],))
    comments = []
    for comment_raw in comments_raw:
        comment = dict(comment_raw)
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    followers_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE followed_id = ?', (user['id'],), one=True)['cnt']
    following_count = query_db('SELECT COUNT(*) as cnt FROM follows WHERE follower_id = ?', (user['id'],), one=True)['cnt']

    #  NEW: CHECK FOLLOW STATUS 
    is_currently_following = False # Default to False
    current_user_id = session.get('user_id')
    
    # We only need to check if a user is logged in
    if current_user_id:
        follow_relation = query_db(
            'SELECT 1 FROM follows WHERE follower_id = ? AND followed_id = ?',
            (current_user_id, user['id']),
            one=True
        )
        if follow_relation:
            is_currently_following = True
    # --

    return render_template('user_profile.html.j2', 
                           user=user, 
                           posts=posts, 
                           comments=comments,
                           followers_count=followers_count, 
                           following_count=following_count,
                           is_following=is_currently_following)
    

@app.route('/u/<username>/followers')
def user_followers(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    followers = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.follower_id = u.id
        WHERE f.followed_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=followers, title="Followers of")

@app.route('/u/<username>/following')
def user_following(username):
    user = query_db('SELECT * FROM users WHERE username = ?', (username,), one=True)
    if not user:
        abort(404)
    following = query_db('''
        SELECT u.username
        FROM follows f
        JOIN users u ON f.followed_id = u.id
        WHERE f.follower_id = ?
    ''', (user['id'],))
    return render_template('user_list.html.j2', user=user, users=following, title="Users followed by")

@app.route('/posts/<int:post_id>')
def post_detail(post_id):
    """Displays a single post and its comments, with content moderation applied."""
    
    post_raw = query_db('''
        SELECT p.id, p.content, p.created_at, u.username, u.id as user_id
        FROM posts p
        JOIN users u ON p.user_id = u.id
        WHERE p.id = ?
    ''', (post_id,), one=True)

    if not post_raw:
        # The abort function will stop the request and show a 404 Not Found page.
        abort(404)

    #  Moderation for the Main Post 
    # Convert the raw database row to a mutable dictionary
    post = dict(post_raw)
    # Unpack the tuple from moderate_content, we only need the moderated content string here
    moderated_post_content, _ = moderate_content(post['content'])
    post['content'] = moderated_post_content

    #  Fetch Reactions (No moderation needed) 
    reactions = query_db('''
        SELECT reaction_type, COUNT(*) as count
        FROM reactions
        WHERE post_id = ?
        GROUP BY reaction_type
    ''', (post_id,))

    #  Fetch and Moderate Comments 
    comments_raw = query_db('SELECT c.id, c.content, c.created_at, u.username, u.id as user_id FROM comments c JOIN users u ON c.user_id = u.id WHERE c.post_id = ? ORDER BY c.created_at ASC', (post_id,))
    
    comments = [] # Create a new list for the moderated comments
    for comment_raw in comments_raw:
        comment = dict(comment_raw) # Convert to a dictionary
        # Moderate the content of each comment
        print(comment['content'])
        moderated_comment_content, _ = moderate_content(comment['content'])
        comment['content'] = moderated_comment_content
        comments.append(comment)

    # Pass the moderated data to the template
    return render_template('post_detail.html.j2',
                           post=post,
                           reactions=reactions,
                           comments=comments,
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES)

@app.route('/about')
def about():
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']
        location = request.form.get('location', '')
        birthdate = request.form.get('birthdate', '')
        profile = request.form.get('profile', '')

        hashed_password = generate_password_hash(password)

        db = get_db()
        cur = db.cursor()
        try:
            cur.execute(
                'INSERT INTO users (username, password, location, birthdate, profile) VALUES (?, ?, ?, ?, ?)',
                (username, hashed_password, location, birthdate, profile)
            )
            db.commit()

            # 1. Get the ID of the user we just created.
            new_user_id = cur.lastrowid

            # 2. Add user info to the session cookie.
            session.clear() # Clear any old session data
            session['user_id'] = new_user_id
            session['username'] = username

            # 3. Flash a welcome message and redirect to the feed.
            flash(f'Welcome, {username}! Your account has been created.', 'success')
            return redirect(url_for('feed')) # Redirect to the main feed/dashboard

        except sqlite3.IntegrityError:
            flash('Username already taken. Please choose another one.', 'danger')
        finally:
            cur.close()
            db.close()
            
    return render_template('signup.html.j2')

@app.route('/login', methods=['GET', 'POST'])
def login():
    if request.method == 'POST':
        username = request.form['username']
        password = request.form['password']

        db = get_db()
        user = db.execute('SELECT * FROM users WHERE username = ?', (username,)).fetchone()
        db.close()

        # 1. Check if the user exists.
        # 2. If user exists, use check_password_hash to securely compare the password.
        #    This function handles the salt and prevents timing attacks.
        if user and check_password_hash(user['password'], password):
            # Password is correct!
            session['user_id'] = user['id']
            session['username'] = user['username']
            flash('Logged in successfully.', 'success')
            return redirect(url_for('feed'))
        else:
            # User does not exist or password was incorrect.
            flash('Invalid username or password.', 'danger')
            
    return render_template('login.html.j2')

@app.route('/logout')
def logout():
    session.clear()
    flash('Logged out.', 'info')
    return redirect(url_for('login'))

@app.route('/posts/<int:post_id>/comment', methods=['POST'])
def add_comment(post_id):
    """Handles adding a new comment to a specific post."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to comment.', 'danger')
        return redirect(url_for('login'))

    # Get content from the submitted form
    content = request.form.get('content', '')

    # Basic validation to ensure comment is not empty
    if not content.strip():
        flash('Comment cannot be empty.', 'warning')
        return redirect(url_for('post_detail', post_id=post_id))

    # Design Claim 20: the same point-of-action reminder as for posts.
    reminder_page = check_before_saving(user_id, content, 'comment', post_id,
                                        form_action=url_for('add_comment', post_id=post_id),
                                        cancel_url=url_for('post_detail', post_id=post_id))
    if reminder_page:
        return reminder_page

    db = get_db()
    db.execute('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
               (post_id, user_id, content))
    db.commit()
    flash('Your comment was added.', 'success')

    # Go back to where the user came from. After a reminder page, go to the post itself.
    if request.form.get('reminder_id'):
        return redirect(url_for('post_detail', post_id=post_id))
    return redirect(request.referrer or url_for('post_detail', post_id=post_id))

@app.route('/comments/<int:comment_id>/delete', methods=['POST'])
def delete_comment(comment_id):
    """Handles deleting a comment."""
    user_id = session.get('user_id')

    # Block access if user is not logged in
    if not user_id:
        flash('You must be logged in to delete a comment.', 'danger')
        return redirect(url_for('login'))

    # Find the comment and the original post's author ID
    comment = query_db('''
        SELECT c.id, c.user_id, p.user_id as post_author_id
        FROM comments c
        JOIN posts p ON c.post_id = p.id
        WHERE c.id = ?
    ''', (comment_id,), one=True)

    # Check if the comment exists
    if not comment:
        flash('Comment not found.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # Security Check: Allow deletion if the user is the comment's author OR the post's author
    if user_id != comment['user_id'] and user_id != comment['post_author_id']:
        flash('You do not have permission to delete this comment.', 'danger')
        return redirect(request.referrer or url_for('feed'))

    # If all checks pass, proceed with deletion
    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()

    flash('Comment successfully deleted.', 'success')
    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/react', methods=['POST'])
def add_reaction():
    """Handles adding a new reaction or updating an existing one."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to react.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')
    new_reaction_type = request.form.get('reaction')

    if not post_id or not new_reaction_type:
        flash("Invalid reaction request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Step 1: Check if a reaction from this user already exists on this post.
    existing_reaction = query_db('SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
                                 (post_id, user_id), one=True)

    if existing_reaction:
        # Step 2: If it exists, UPDATE the reaction_type.
        db.execute('UPDATE reactions SET reaction_type = ? WHERE id = ?',
                   (new_reaction_type, existing_reaction['id']))
    else:
        # Step 3: If it does not exist, INSERT a new reaction.
        db.execute('INSERT INTO reactions (post_id, user_id, reaction_type) VALUES (?, ?, ?)',
                   (post_id, user_id, new_reaction_type))

    db.commit()

    return redirect(request.referrer or url_for('feed'))

@app.route('/unreact', methods=['POST'])
def unreact():
    """Handles removing a user's reaction from a post."""
    user_id = session.get('user_id')

    if not user_id:
        flash("You must be logged in to unreact.", "danger")
        return redirect(url_for('login'))

    post_id = request.form.get('post_id')

    if not post_id:
        flash("Invalid unreact request.", "warning")
        return redirect(request.referrer or url_for('feed'))

    db = get_db()

    # Remove the reaction if it exists
    existing_reaction = query_db(
        'SELECT id FROM reactions WHERE post_id = ? AND user_id = ?',
        (post_id, user_id),
        one=True
    )

    if existing_reaction:
        db.execute('DELETE FROM reactions WHERE id = ?', (existing_reaction['id'],))
        db.commit()
        flash("Reaction removed.", "success")
    else:
        flash("No reaction to remove.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/follow', methods=['POST'])
def follow_user(user_id):
    """Handles the logic for the current user to follow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to follow users.", "danger")
        return redirect(url_for('login'))

    # Security: Prevent users from following themselves
    if follower_id == user_id:
        flash("You cannot follow yourself.", "warning")
        return redirect(request.referrer or url_for('feed'))

    # Check if the user to be followed actually exists
    user_to_follow = query_db('SELECT id FROM users WHERE id = ?', (user_id,), one=True)
    if not user_to_follow:
        flash("The user you are trying to follow does not exist.", "danger")
        return redirect(request.referrer or url_for('feed'))
        
    db = get_db()
    try:
        # Insert the follow relationship. The PRIMARY KEY constraint will prevent duplicates if you've set one.
        db.execute('INSERT INTO follows (follower_id, followed_id) VALUES (?, ?)',
                   (follower_id, user_id))
        db.commit()
        username_to_follow = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You are now following {username_to_follow}.", "success")
    except sqlite3.IntegrityError:
        flash("You are already following this user.", "info")

    return redirect(request.referrer or url_for('feed'))


@app.route('/u/<int:user_id>/unfollow', methods=['POST'])
def unfollow_user(user_id):
    """Handles the logic for the current user to unfollow another user."""
    follower_id = session.get('user_id')

    # Security: Ensure user is logged in
    if not follower_id:
        flash("You must be logged in to unfollow users.", "danger")
        return redirect(url_for('login'))

    db = get_db()
    cur = db.execute('DELETE FROM follows WHERE follower_id = ? AND followed_id = ?',
               (follower_id, user_id))
    db.commit()

    if cur.rowcount > 0:
        # cur.rowcount tells us if a row was actually deleted
        username_unfollowed = query_db('SELECT username FROM users WHERE id = ?', (user_id,), one=True)['username']
        flash(f"You have unfollowed {username_unfollowed}.", "success")
    else:
        # This case handles if someone tries to unfollow a user they weren't following
        flash("You were not following this user.", "info")

    # Redirect back to the page the user came from
    return redirect(request.referrer or url_for('feed'))

@app.route('/admin')
def admin_dashboard():
    """Displays the admin dashboard with users, posts, and comments, sorted by risk."""

    if session.get('username') != 'admin':
        flash("You do not have permission to access this page.", "danger")
        return redirect(url_for('feed'))

    RISK_LEVELS = { "HIGH": 5, "MEDIUM": 3, "LOW": 1 }
    PAGE_SIZE = 50

    def get_risk_profile(score):
        if score >= RISK_LEVELS["HIGH"]:
            return "HIGH", 3
        elif score >= RISK_LEVELS["MEDIUM"]:
            return "MEDIUM", 2
        elif score >= RISK_LEVELS["LOW"]:
            return "LOW", 1
        return "NONE", 0

    # Get pagination and current tab parameters
    try:
        users_page = int(request.args.get('users_page', 1))
        posts_page = int(request.args.get('posts_page', 1))
        comments_page = int(request.args.get('comments_page', 1))
    except ValueError:
        users_page = 1
        posts_page = 1
        comments_page = 1
    
    current_tab = request.args.get('tab', 'users') # Default to 'users' tab

    users_offset = (users_page - 1) * PAGE_SIZE
    
    # First, get all users to calculate risk, then apply pagination in Python
    # It's more complex to do this efficiently in SQL if risk calc is Python-side
    all_users_raw = query_db('SELECT id, username, profile, created_at FROM users')
    all_users = []
    for user in all_users_raw:
        user_dict = dict(user)
        user_risk_score = user_risk_analysis(user_dict['id'])
        risk_label, risk_sort_key = get_risk_profile(user_risk_score)
        user_dict['risk_label'] = risk_label
        user_dict['risk_sort_key'] = risk_sort_key
        user_dict['risk_score'] = min(5.0, round(user_risk_score, 2))
        all_users.append(user_dict)

    all_users.sort(key=lambda x: x['risk_score'], reverse=True)
    total_users = len(all_users)
    users = all_users[users_offset : users_offset + PAGE_SIZE]
    total_users_pages = (total_users + PAGE_SIZE - 1) // PAGE_SIZE

    # --- Posts Tab Data ---
    posts_offset = (posts_page - 1) * PAGE_SIZE
    total_posts_count = query_db('SELECT COUNT(*) as count FROM posts', one=True)['count']
    total_posts_pages = (total_posts_count + PAGE_SIZE - 1) // PAGE_SIZE

    posts_raw = query_db(f'''
        SELECT p.id, p.content, p.created_at, u.username, u.created_at as user_created_at
        FROM posts p JOIN users u ON p.user_id = u.id
        ORDER BY p.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, posts_offset))
    posts = []
    for post in posts_raw:
        post_dict = dict(post)
        _, base_score = moderate_content(post_dict['content'])
        final_score = base_score 
        author_created_dt = post_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            final_score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(final_score)
        post_dict['risk_label'] = risk_label
        post_dict['risk_sort_key'] = risk_sort_key
        post_dict['risk_score'] = round(final_score, 2)
        posts.append(post_dict)

    posts.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring

    # --- Comments Tab Data ---
    comments_offset = (comments_page - 1) * PAGE_SIZE
    total_comments_count = query_db('SELECT COUNT(*) as count FROM comments', one=True)['count']
    total_comments_pages = (total_comments_count + PAGE_SIZE - 1) // PAGE_SIZE

    comments_raw = query_db(f'''
        SELECT c.id, c.content, c.created_at, u.username, u.created_at as user_created_at
        FROM comments c JOIN users u ON c.user_id = u.id
        ORDER BY c.id DESC -- Order by ID for consistent pagination before risk sort
        LIMIT ? OFFSET ?
    ''', (PAGE_SIZE, comments_offset))
    comments = []
    for comment in comments_raw:
        comment_dict = dict(comment)
        _, score = moderate_content(comment_dict['content'])
        author_created_dt = comment_dict['user_created_at']
        author_age_days = (datetime.utcnow() - author_created_dt).days
        if author_age_days < 7:
            score *= 1.5
        risk_label, risk_sort_key = get_risk_profile(score)
        comment_dict['risk_label'] = risk_label
        comment_dict['risk_sort_key'] = risk_sort_key
        comment_dict['risk_score'] = round(score, 2)
        comments.append(comment_dict)

    comments.sort(key=lambda x: x['risk_score'], reverse=True) # Sort after fetching and scoring


    return render_template('admin.html.j2', 
                           users=users, 
                           posts=posts, 
                           comments=comments,
                           
                           # Pagination for Users
                           users_page=users_page,
                           total_users_pages=total_users_pages,
                           users_has_next=(users_page < total_users_pages),
                           users_has_prev=(users_page > 1),

                           # Pagination for Posts
                           posts_page=posts_page,
                           total_posts_pages=total_posts_pages,
                           posts_has_next=(posts_page < total_posts_pages),
                           posts_has_prev=(posts_page > 1),

                           # Pagination for Comments
                           comments_page=comments_page,
                           total_comments_pages=total_comments_pages,
                           comments_has_next=(comments_page < total_comments_pages),
                           comments_has_prev=(comments_page > 1),

                           current_tab=current_tab,
                           reminders=reminder_statistics(),  # Design Claim 20 numbers
                           PAGE_SIZE=PAGE_SIZE)



@app.route('/admin/delete/user/<int:user_id>', methods=['POST'])
def admin_delete_user(user_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))
        
    if user_id == session.get('user_id'):
        flash('You cannot delete your own account from the admin panel.', 'danger')
        return redirect(url_for('admin_dashboard'))
    
    db = get_db()
    db.execute('DELETE FROM users WHERE id = ?', (user_id,))
    db.commit()
    flash(f'User {user_id} and all their content has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/post/<int:post_id>', methods=['POST'])
def admin_delete_post(post_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM reactions WHERE post_id = ?', (post_id,))
    db.execute('DELETE FROM posts WHERE id = ?', (post_id,))
    db.commit()
    flash(f'Post {post_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))


@app.route('/admin/delete/comment/<int:comment_id>', methods=['POST'])
def admin_delete_comment(comment_id):
    if session.get('username') != 'admin':
        flash("You do not have permission to perform this action.", "danger")
        return redirect(url_for('feed'))

    db = get_db()
    db.execute('DELETE FROM comments WHERE id = ?', (comment_id,))
    db.commit()
    flash(f'Comment {comment_id} has been deleted.', 'success')
    return redirect(url_for('admin_dashboard'))

@app.route('/rules')
def rules():
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Design Claim feature (Coding Assignment #2) -----
# Chapter 4, Design Claim 20: "Offering people reminders at the point of an action
# that may violate norms will reduce the number of offenses."
#
# How it works: when someone submits a post or comment, we run the moderation
# rules on the draft BEFORE it is saved. If it would be censored or removed, the
# user sees a short reminder page instead: which rule it breaks (in plain words),
# how others would see it, and a chance to edit it, post it anyway, or cancel.
# Every reminder and what the user did next is saved in the table
# norm_reminders, so the admin can see if reminders really reduce offenses.

def create_reminder_table():
    """Creates the norm_reminders table once, if it does not exist yet."""
    with sqlite3.connect(DATABASE) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS norm_reminders (
                id            INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id       INTEGER NOT NULL,
                content_type  TEXT    NOT NULL,          -- 'post' or 'comment'
                post_id       INTEGER,                   -- the post a comment belongs to
                score_before  REAL    NOT NULL,          -- score of the first draft
                score_after   REAL,                      -- score of what was finally posted
                outcome       TEXT    NOT NULL DEFAULT 'pending',
                                                         -- pending / edited_clean / edited_still_flagged
                                                         -- / posted_anyway / cancelled
                created_at    TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )''')

create_reminder_table()


def explain_moderation(details):
    """Turns the result of analyse_content() into short, friendly reasons."""
    reasons = []
    if details['removed'] == 'severe':
        reasons.append(('Severe language (Rule 1.1.1)',
                        'It contains a word that is not allowed here. The whole text would be removed.'))
    elif details['removed'] == 'spam':
        reasons.append(('Spam or scam (Rule 1.1.2)',
                        'It contains a phrase we often see in spam or scams. The whole text would be removed.'))
    if details['tier3']:
        reasons.append(('Rude words (Rule 1.2.1)',
                        f"{details['tier3']} word(s) would be hidden with ***."))
    if details['links']:
        reasons.append(('Links (Rule 1.2.2)',
                        f"{details['links']} link(s) would be replaced with [link removed]."))
    if details['caps']:
        reasons.append(('Shouting (Rule 1.2.3)',
                        'Most of the text is in CAPITALS, which reads as shouting.'))
    return reasons


def get_own_reminder(reminder_id, user_id):
    """Returns the reminder only if it belongs to this user (so nobody can edit others' records)."""
    if not reminder_id:
        return None
    return query_db('SELECT * FROM norm_reminders WHERE id = ? AND user_id = ?',
                    (reminder_id, user_id), one=True)


def check_before_saving(user_id, content, content_type, post_id, form_action, cancel_url):
    """
    The point-of-action reminder (DC20).

    Returns None when the content may be saved now.
    Returns a rendered reminder page when the user should look at it again first.
    """
    decision = request.form.get('decision')          # 'recheck', 'post_anyway' or None
    reminder = get_own_reminder(request.form.get('reminder_id', type=int), user_id)
    details = analyse_content(content)

    # The user saw the reminder and chose to post anyway: respect the choice.
    # The normal moderation still applies when the post is shown.
    if reminder and decision == 'post_anyway':
        query_db('UPDATE norm_reminders SET outcome = ?, score_after = ? WHERE id = ?',
                 ('posted_anyway', details['score'], reminder['id']), commit=True)
        return None

    # Clean text: save it. If it was fixed after a reminder, record that success.
    if details['score'] == 0:
        if reminder:
            query_db('UPDATE norm_reminders SET outcome = ?, score_after = 0 WHERE id = ?',
                     ('edited_clean', reminder['id']), commit=True)
        return None

    # The text would be moderated: show the reminder (first time, or again after an edit).
    if reminder:
        query_db('UPDATE norm_reminders SET outcome = ?, score_after = ? WHERE id = ?',
                 ('edited_still_flagged', details['score'], reminder['id']), commit=True)
        reminder_id = reminder['id']
    else:
        cur = query_db('INSERT INTO norm_reminders (user_id, content_type, post_id, score_before) '
                       'VALUES (?, ?, ?, ?)', (user_id, content_type, post_id, details['score']), commit=True)
        reminder_id = cur.lastrowid

    author = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    is_new_account = author is not None and account_age_days(author['created_at']) < 7

    return render_template('review_content.html.j2',
                           content_type=content_type,
                           original=content,
                           preview=details['moderated'],
                           removed=details['removed'],
                           reasons=explain_moderation(details),
                           is_new_account=is_new_account,
                           reminder_id=reminder_id,
                           form_action=form_action,
                           cancel_url=url_for('cancel_reminder', reminder_id=reminder_id, next=cancel_url))


@app.route('/reminders/<int:reminder_id>/cancel')
def cancel_reminder(reminder_id):
    """The user decided not to post after seeing the reminder."""
    user_id = session.get('user_id')
    if get_own_reminder(reminder_id, user_id):
        # Only open reminders can be cancelled; finished ones keep their result.
        query_db("UPDATE norm_reminders SET outcome = 'cancelled' "
                 "WHERE id = ? AND outcome IN ('pending', 'edited_still_flagged')",
                 (reminder_id,), commit=True)
        flash('Okay, nothing was posted.', 'info')
    next_url = request.args.get('next', '')
    # Only allow redirects inside this site.
    if not next_url.startswith('/') or next_url.startswith('//'):
        next_url = url_for('feed')
    return redirect(next_url)


def reminder_statistics():
    """Numbers for the admin dashboard: do reminders reduce offenses?"""
    rows = query_db('SELECT outcome, COUNT(*) AS n FROM norm_reminders GROUP BY outcome') or []
    counts = {row['outcome']: row['n'] for row in rows}
    total = sum(counts.values())
    fixed = counts.get('edited_clean', 0) + counts.get('cancelled', 0)
    return {
        'total': total,
        'edited_clean': counts.get('edited_clean', 0),
        'cancelled': counts.get('cancelled', 0),
        'posted_anyway': counts.get('posted_anyway', 0),
        'open': counts.get('pending', 0) + counts.get('edited_still_flagged', 0),
        'prevented_pct': round(100 * fixed / total) if total else 0,
    }


# ----- Functions to be implemented are below
# Coding Assignment #2

# ----- Moderation helpers (Coding Assignment #2) -----

REMOVED_SEVERE_TEXT = '[content removed due to severe violation]'
REMOVED_SPAM_TEXT = '[content removed due to spam/scam policy]'

def build_phrase_pattern(phrases):
    """
    Builds one search pattern that finds any word/phrase from the list,
    ignoring upper/lower case, and only as a WHOLE word or phrase.
    (?<!\\w) means "no letter or digit right before", (?!\\w) means "none right after".
    So a bad word hidden inside a longer, normal word is NOT matched.
    Longer phrases are tried first, so the longest match wins.
    """
    cleaned = sorted({p.strip() for p in phrases if p and p.strip()}, key=len, reverse=True)
    if not cleaned:
        return None
    return re.compile(r'(?<!\w)(?:' + '|'.join(re.escape(p) for p in cleaned) + r')(?!\w)',
                      re.IGNORECASE)

# Built once when the app starts, so moderation stays fast.
TIER1_PATTERN = build_phrase_pattern(TIER1_WORDS)
TIER2_PATTERN = build_phrase_pattern(TIER2_PHRASES)
TIER3_PATTERN = build_phrase_pattern(TIER3_WORDS)

# A link starts with http://, https:// or www. and runs until the next space.
URL_PATTERN = re.compile(r'(?:https?://|www\.)[^\s<>"]+', re.IGNORECASE)

def account_age_days(created_at):
    """How many whole days old an account is. Unknown dates count as old accounts."""
    if created_at is None:
        return 10**6
    if isinstance(created_at, str):
        try:
            created_at = datetime.fromisoformat(created_at)
        except ValueError:
            return 10**6
    return (datetime.utcnow() - created_at).days


# Assignment 2.2
def user_risk_analysis(user_id):
    """
    Gives one user a risk score, following Rule 2.2 on the /rules page.

    Args:
        user_id: The ID of the user on which we perform risk analysis.

    Returns:
        A float between 0.0 and 5.0.
        Below 1.0 = NONE, 1.0-2.99 = LOW, 3.0-4.99 = MEDIUM, 5.0 = HIGH.

    Steps (same numbers as on the rules page):
        1. profile_score         = content score of the profile text
        2. average_post_score    = average content score of all the user's posts (0 if none)
        3. average_comment_score = average content score of all the user's comments (0 if none)
        4. content_risk_score    = profile * 1 + average post * 3 + average comment * 1
        5. age multiplier        = x1.5 if the account is under 7 days old,
                                   x1.2 if under 30 days, otherwise x1.0
        6. cap the result at 5.0
    Note: steps 1-3 use the plain content score from moderate_content().
    The x1.5 rule for single posts (Rule 2.1) is NOT used here; step 5 handles account age.
    """
    user = query_db('SELECT profile, created_at FROM users WHERE id = ?', (user_id,), one=True)
    if user is None:
        return 0.0

    # Step 1: profile score (an empty profile scores 0)
    _, profile_score = moderate_content(user['profile'] or '')

    # Step 2: average post score
    posts = query_db('SELECT content FROM posts WHERE user_id = ?', (user_id,))
    if posts:
        average_post_score = sum(moderate_content(p['content'])[1] for p in posts) / len(posts)
    else:
        average_post_score = 0.0

    # Step 3: average comment score
    comments = query_db('SELECT content FROM comments WHERE user_id = ?', (user_id,))
    if comments:
        average_comment_score = sum(moderate_content(c['content'])[1] for c in comments) / len(comments)
    else:
        average_comment_score = 0.0

    # Step 4: combine. Posts count three times as much as the profile or comments.
    content_risk_score = (profile_score * 1) + (average_post_score * 3) + (average_comment_score * 1)

    # Step 5: newer accounts get a higher multiplier
    age_days = account_age_days(user['created_at'])
    if age_days < 7:
        user_risk_score = content_risk_score * 1.5
    elif age_days < 30:
        user_risk_score = content_risk_score * 1.2
    else:
        user_risk_score = content_risk_score

    # Step 6: never higher than 5.0
    return min(5.0, user_risk_score)


# Assignment 2.1
def moderate_content(content):
    """
    Moderates one post, comment or profile text, following Part 1 of the /rules page.

    Args
        content: the text content of a post, comment or profile.

    Returns:
        A tuple (moderated_content, score).
        moderated_content is the text other users will see.
        score is the Content Score (0.0 = clean, 5.0 = removed).

    The real work is done in analyse_content() below, which also returns the
    details (which rules were broken). The design-claim feature uses those details.
    """
    result = analyse_content(content)
    return result['moderated'], result['score']


def analyse_content(content):
    """
    Applies the moderation rules and remembers which rules were broken.

    Returns a dict:
        moderated  - the text after filtering
        score      - the Content Score
        removed    - None, 'severe' (Tier 1) or 'spam' (Tier 2)
        tier3      - how many mild bad words were masked
        links      - how many links were removed
        caps       - True if the text is mostly CAPITALS
    """
    result = {'moderated': content or '', 'score': 0.0,
              'removed': None, 'tier3': 0, 'links': 0, 'caps': False}
    if not content:
        return result

    # Stage 1.1 - severe checks. A match removes the whole text and stops here.
    # Rule 1.1.1: Tier 1 word (whole word, any case)
    if TIER1_PATTERN and TIER1_PATTERN.search(content):
        result.update(moderated=REMOVED_SEVERE_TEXT, score=5.0, removed='severe')
        return result
    # Rule 1.1.2: Tier 2 spam/scam phrase (whole phrase, any case)
    if TIER2_PATTERN and TIER2_PATTERN.search(content):
        result.update(moderated=REMOVED_SPAM_TEXT, score=5.0, removed='spam')
        return result

    # Stage 1.2 - scored checks. The score starts at 0.0.
    moderated = content
    score = 0.0

    # Rule 1.2.1: each Tier 3 word -> same number of asterisks, +2.0 each
    if TIER3_PATTERN:
        moderated, tier3_count = TIER3_PATTERN.subn(lambda m: '*' * len(m.group(0)), moderated)
        score += 2.0 * tier3_count
        result['tier3'] = tier3_count

    # Rule 1.2.2: each link -> "[link removed]", +2.0 each
    moderated, link_count = URL_PATTERN.subn('[link removed]', moderated)
    score += 2.0 * link_count
    result['links'] = link_count

    # Rule 1.2.3: more than 15 letters and over 70% of them CAPITALS -> +0.5 (text unchanged)
    # We count letters in the original text, so "[link removed]" does not change the count.
    letters = [ch for ch in content if ch.isalpha()]
    if len(letters) > 15:
        upper_share = sum(1 for ch in letters if ch.isupper()) / len(letters)
        if upper_share > 0.7:
            score += 0.5
            result['caps'] = True

    result['moderated'] = moderated
    result['score'] = score
    return result

# Coding Assignment #3
# Assignment 3.1
def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.
    
    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113
    
    Materials: 
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """

    recommended_posts = {} 

    return recommended_posts;

if __name__ == '__main__':
    app.run(debug=True, port=8080)

