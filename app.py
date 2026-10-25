from flask import Flask, render_template, request, redirect, url_for, session, flash, g, abort
from werkzeug.security import generate_password_hash, check_password_hash
from cryptography.fernet import Fernet
import collections
import json
import sqlite3
import hashlib
import re
import math
from datetime import datetime, timedelta

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
    """Shows a date from the database in a short, readable format on the pages."""
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
    """The main page: shows posts with the chosen sort, filter and page, plus the extra cards on top."""
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
    elif sort == 'waiting' and current_user_id:
        # Coding Assignment #1, feature 1 (Ch2 DC1): only the posts from people
        # you follow that still need your reaction or comment, newest first.
        posts = waiting_posts(current_user_id, POSTS_PER_PAGE, offset)
    else:  # Default sort is 'new'
        sort = 'new'  # also used when a logged-out visitor asks for 'waiting'
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

    #  Coding Assignment #1: data for the three new feed cards (logged-in users only)
    waiting_total, waiting_preview, goal, received = 0, [], None, None
    if current_user_id:
        waiting_total = waiting_count(current_user_id)            # feature 1 (Ch2 DC1)
        if sort != 'waiting' and page == 1:
            for row in waiting_posts(current_user_id, 3):          # first 3, shown at the top
                preview = dict(row)
                preview['content'], _ = moderate_content(preview['content'])
                waiting_preview.append(preview)
        goal = goal_status(current_user_id)                       # feature 2 (Ch2 DC13, DC15)
        received = received_totals(current_user_id)               # feature 3 (Ch3 DC30)
    # Coding Assignment #3: the newcomer journey card (Ch5 DC22, DC20)
    journey = journey_status(current_user_id) if current_user_id and is_newcomer(current_user_id) else None

    #  4. Render Template with Pagination Info 
    return render_template('feed.html.j2', 
                           posts=posts_data, 
                           current_sort=sort,
                           current_show=show,
                           page=page, # Pass current page number
                           per_page=POSTS_PER_PAGE, # Pass items per page
                           reaction_emojis=REACTION_EMOJIS,
                           reaction_types=REACTION_TYPES,
                           waiting_total=waiting_total,
                           waiting_preview=waiting_preview,
                           goal=goal,
                           received=received,
                           journey=journey)

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
    flash('Your post was successfully created!' + goal_feedback(user_id, 'post')
          + journey_after_post(user_id, content), 'success')     # C3: step 1 of the newcomer journey

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
    """Lists the people who follow this user."""
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
    """Lists the people this user follows."""
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
    """Shows the About page."""
    return render_template('about.html.j2')

@app.route('/privacy')
def privacy():
    """Shows the Privacy page."""
    return render_template('privacy.html.j2')


@app.route('/signup', methods=['GET', 'POST'])
def signup():
    """Creates a new account and logs the new user in."""
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
    """Checks the username and password and logs the user in."""
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
    """Logs the user out and goes back to the login page."""
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
    note_welcome(user_id, post_id)     # C3: a first contact's comment counts as the welcome
    flash('Your comment was added.' + goal_feedback(user_id, 'comment')
          + journey_after_comment(user_id, post_id), 'success')  # C3: step 3 of the journey

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

    # Coding Assignment #3: a reaction can be step 2 of the newcomer journey
    message = journey_after_reaction(user_id, int(post_id), new_reaction_type)
    if message:
        flash(message, 'success')

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
        """Turns a risk score into a level (HIGH, MEDIUM, LOW or NONE) and a sort number."""
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
    """Admin only: deletes a user account."""
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
    """Admin only: deletes a post."""
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
    """Admin only: deletes a comment."""
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
    """Shows the community rules page."""
    return render_template('rules.html.j2')

@app.template_global()
def loop_color(user_id):
    # Generate a pastel color based on user_id hash
    """Gives every user their own soft colour, used for their user icon and name."""
    h = hashlib.md5(str(user_id).encode()).hexdigest()
    r = int(h[0:2], 16)
    g = int(h[2:4], 16)
    b = int(h[4:6], 16)
    return f'rgb({r % 128 + 80}, {g % 128 + 80}, {b % 128 + 80})'


# ----- Coding Assignment #1: Contribution and Commitment -----
# Three new features. Each one implements design claims from the course book
# (Kraut & Resnick, "Building Successful Online Communities").
#
# 1. "Waiting for you" - Chapter 2, Design Claim 1:
#    "Making the list of needed contributions easily visible increases the
#     likelihood that the community will provide them."
#    Here a needed contribution = a reaction or a comment on a post from someone
#    you follow. Posts you have not answered yet are shown first, with a counter.
#
# 2. "Weekly goal" - Chapter 2, Design Claims 13 and 15:
#    specific and challenging goals increase contribution (DC13), and goals work
#    better with frequent feedback (DC15). The user sets a weekly goal (or takes
#    the suggested one), sees progress bars and gets feedback after every post
#    or comment. A comparison with other members shows how the week is going.
#
# 3. "What the community gave you" - Chapter 3, Design Claims 30 and 31:
#    showing people what they have received from the community increases their
#    normative commitment (DC30), and highlighting chances to return favours to
#    specific people increases it too (DC31).

# Reactions that count as support. The seed data uses 'haha', the UI uses 'laugh'.
POSITIVE_REACTIONS = ('like', 'love', 'laugh', 'haha', 'wow')
POSITIVE_SQL = "('like', 'love', 'laugh', 'haha', 'wow')"   # the same list, for SQL


def create_goal_table():
    """Creates the weekly_goals table once, if it does not exist yet."""
    with sqlite3.connect(DATABASE) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS weekly_goals (
                user_id          INTEGER PRIMARY KEY,
                posts_goal       INTEGER NOT NULL,
                comments_goal    INTEGER NOT NULL,
                took_suggestion  INTEGER NOT NULL DEFAULT 0,   -- 1 = the user took the suggested goal
                updated_at       TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )''')

create_goal_table()


def safe_next_url(url, fallback):
    """Only allow redirects inside this site."""
    if url and url.startswith('/') and not url.startswith('//'):
        return url
    return fallback


# --- Feature 1: "Waiting for you" (Chapter 2, Design Claim 1) ---

# Posts written by people the user follows, which the user has not reacted to
# and has not commented on yet. The four ? are all the current user's id.
WAITING_FROM_WHERE = '''
    FROM posts p
    JOIN users u ON p.user_id = u.id
    WHERE p.user_id IN (SELECT followed_id FROM follows WHERE follower_id = ?)
      AND p.user_id != ?
      AND NOT EXISTS (SELECT 1 FROM reactions r WHERE r.post_id = p.id AND r.user_id = ?)
      AND NOT EXISTS (SELECT 1 FROM comments c WHERE c.post_id = p.id AND c.user_id = ?)
'''


def waiting_count(user_id):
    """
    Counts how many posts are still waiting for this user.

    A post is "waiting" when someone the user follows wrote it, and the user
    has not reacted to it or commented on it yet (see WAITING_FROM_WHERE above).

    The feed shows this number in the "Waiting for you" box and on the
    "Waiting for you (3)" sort button. Seeing that people they follow still
    have no answer reminds the user that their reply matters (Design Claim 1).

    Returns 0 when nothing is waiting.
    """
    # The same user id fills all four ? in the query, so we repeat it 4 times.
    row = query_db('SELECT COUNT(*) AS n ' + WAITING_FROM_WHERE, (user_id,) * 4, one=True)
    return row['n'] if row else 0


def waiting_posts(user_id, limit, offset=0):
    """
    Gets the waiting posts themselves, so the user can answer them.

    It uses the same rule as waiting_count(), but returns the posts instead of
    a number. The newest posts come first.

    It is used in two places in feed():
      - the 3 newest waiting posts are shown in a box at the top of the feed,
      - the "Waiting for you" sort shows all of them, one page at a time.

    Args:
        user_id: the user who is logged in.
        limit:   how many posts to return (for one page, or 3 for the box).
        offset:  how many posts to skip first (used for page 2, 3, ...).

    The posts have the same columns as the normal feed, so the feed page can
    show them in the same way. Returns an empty list when nothing is waiting.
    """
    # Four times the user id for the four ?, then the page size and the offset.
    rows = query_db('SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id '
                    + WAITING_FROM_WHERE + ' ORDER BY p.created_at DESC LIMIT ? OFFSET ?',
                    (user_id,) * 4 + (limit, offset))
    return rows or []


# --- Feature 2: "Weekly goal" (Chapter 2, Design Claims 13 and 15) ---

def start_of_week():
    """Monday 00:00 of this week as text, in the same format (UTC) as the database."""
    today = datetime.utcnow().date()
    monday = today - timedelta(days=today.weekday())
    return monday.strftime('%Y-%m-%d 00:00:00')


def count_this_week(user_id):
    """How many posts and comments the user has written since Monday."""
    since = start_of_week()
    posts = query_db('SELECT COUNT(*) AS n FROM posts WHERE user_id = ? AND created_at >= ?',
                     (user_id, since), one=True)['n']
    comments = query_db('SELECT COUNT(*) AS n FROM comments WHERE user_id = ? AND created_at >= ?',
                        (user_id, since), one=True)['n']
    return posts, comments


def usual_week(user_id):
    """The user's usual week: average posts and comments per week in which they were active."""
    def per_active_week(table):
        # table is always 'posts' or 'comments' (fixed names, never user input)
        row = query_db(f"SELECT COUNT(*) AS n, COUNT(DISTINCT strftime('%Y-%W', created_at)) AS weeks "
                       f"FROM {table} WHERE user_id = ?", (user_id,), one=True)
        return row['n'] / row['weeks'] if row and row['weeks'] else 0.0
    return per_active_week('posts'), per_active_week('comments')


def suggest_goal(user_id):
    """
    A specific and challenging goal (DC13): about 50% more than the user's usual
    week, but at least 2 posts and 5 comments, so new users also get a real goal.
    """
    usual_posts, usual_comments = usual_week(user_id)
    posts_goal = min(20, max(2, math.ceil(usual_posts * 1.5)))
    comments_goal = min(50, max(5, math.ceil(usual_comments * 1.5)))
    return posts_goal, comments_goal


def compare_with_members(posts_this_week):
    """
    Comparative feedback. Looks at the usual week of every member who has posted
    (posts per active week) and returns:
      - ahead_pct:     share of members whose usual week has fewer posts than this week
      - average:       the average usual week
      - posts_to_pass: how many more posts this user needs to pass a typical (median) member
    """
    rows = query_db("SELECT COUNT(*) * 1.0 / COUNT(DISTINCT strftime('%Y-%W', created_at)) AS per_week "
                    "FROM posts GROUP BY user_id") or []
    if not rows:
        return 0, 0.0, 0
    weeks = sorted(row['per_week'] for row in rows)
    behind = sum(1 for value in weeks if value < posts_this_week)
    median = weeks[len(weeks) // 2]
    posts_to_pass = max(0, math.floor(median) + 1 - posts_this_week)
    return round(100 * behind / len(weeks)), round(sum(weeks) / len(weeks), 1), posts_to_pass


def goal_status(user_id):
    """Everything the goal card and the goal page need, in one dictionary."""
    goal = query_db('SELECT * FROM weekly_goals WHERE user_id = ?', (user_id,), one=True)
    posts_done, comments_done = count_this_week(user_id)
    suggested_posts, suggested_comments = suggest_goal(user_id)
    usual_posts, usual_comments = usual_week(user_id)
    ahead_pct, community_avg, posts_to_pass = compare_with_members(posts_done)
    status = {
        'has_goal': goal is not None,
        'posts_done': posts_done,
        'comments_done': comments_done,
        'suggested_posts': suggested_posts,
        'suggested_comments': suggested_comments,
        'usual_posts': round(usual_posts, 1),
        'usual_comments': round(usual_comments, 1),
        'ahead_pct': ahead_pct,
        'community_avg': community_avg,
        'posts_to_pass': posts_to_pass,
        'days_left': 7 - datetime.utcnow().weekday(),   # today counts as a day left
    }
    if goal:
        status.update({
            'posts_goal': goal['posts_goal'],
            'comments_goal': goal['comments_goal'],
            'posts_pct': min(100, round(100 * posts_done / goal['posts_goal'])),
            'comments_pct': min(100, round(100 * comments_done / goal['comments_goal'])),
            'reached': posts_done >= goal['posts_goal'] and comments_done >= goal['comments_goal'],
        })
    return status


def goal_feedback(user_id, kind):
    """
    A short progress line added to the message after a new post or comment.
    This is the frequent feedback of Design Claim 15. Empty if there is no goal.
    """
    goal = query_db('SELECT posts_goal, comments_goal FROM weekly_goals WHERE user_id = ?',
                    (user_id,), one=True)
    if not goal:
        return ''
    posts_done, comments_done = count_this_week(user_id)
    if kind == 'post':
        done, target, word = posts_done, goal['posts_goal'], 'posts'
    else:
        done, target, word = comments_done, goal['comments_goal'], 'comments'
    if done == target:
        return f' You reached your weekly goal of {target} {word}!'
    if done > target:
        return f' Weekly goal: {done}/{target} {word}. You are above your goal.'
    return f' Weekly goal: {done}/{target} {word}.'


@app.route('/goals', methods=['GET', 'POST'])
def weekly_goal():
    """Shows the weekly goal page, and saves a new goal."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to set a goal.', 'danger')
        return redirect(url_for('login'))

    if request.method == 'POST':
        if request.form.get('use_suggestion'):
            posts_goal, comments_goal = suggest_goal(user_id)
            took_suggestion = 1
        else:
            posts_goal = request.form.get('posts_goal', type=int)
            comments_goal = request.form.get('comments_goal', type=int)
            took_suggestion = 0
            if not posts_goal or not comments_goal or not (1 <= posts_goal <= 50) \
                    or not (1 <= comments_goal <= 100):
                flash('Please choose 1-50 posts and 1-100 comments.', 'warning')
                return redirect(url_for('weekly_goal'))
        # Insert the goal, or update it if the user already has one
        query_db('INSERT INTO weekly_goals (user_id, posts_goal, comments_goal, took_suggestion) '
                 'VALUES (?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET '
                 'posts_goal = excluded.posts_goal, comments_goal = excluded.comments_goal, '
                 'took_suggestion = excluded.took_suggestion, updated_at = CURRENT_TIMESTAMP',
                 (user_id, posts_goal, comments_goal, took_suggestion), commit=True)
        flash(f'Your weekly goal is set: {posts_goal} posts and {comments_goal} comments.', 'success')
        return redirect(safe_next_url(request.form.get('next'), url_for('weekly_goal')))

    return render_template('goals.html.j2', goal=goal_status(user_id))


# --- Feature 3: "What the community gave you" (Chapter 3, Design Claims 30 and 31) ---

def received_totals(user_id):
    """Reactions and comments that OTHER people gave to this user's posts."""
    reactions = query_db('SELECT COUNT(*) AS n FROM reactions r JOIN posts p ON r.post_id = p.id '
                         'WHERE p.user_id = ? AND r.user_id != ?', (user_id, user_id), one=True)['n']
    comments = query_db('SELECT COUNT(*) AS n FROM comments c JOIN posts p ON c.post_id = p.id '
                        'WHERE p.user_id = ? AND c.user_id != ?', (user_id, user_id), one=True)['n']
    return {'reactions': reactions, 'comments': comments}


def next_post_to_return(user_id, supporter_id):
    """The supporter's newest post that this user has not reacted to yet (a favour to return)."""
    row = query_db('SELECT id FROM posts WHERE user_id = ? AND id NOT IN '
                   '(SELECT post_id FROM reactions WHERE user_id = ?) ORDER BY created_at DESC LIMIT 1',
                   (supporter_id, user_id), one=True)
    return row['id'] if row else None


def top_supporters(user_id, limit=8):
    """
    The people who supported this user most (positive reactions + comments on
    their posts), and how much this user has given back to each of them.
    """
    rows = query_db(f'''
        SELECT u.id, u.username,
               SUM(s.kind = 'reaction') AS reactions,
               SUM(s.kind = 'comment')  AS comments
        FROM (
            SELECT r.user_id AS supporter, 'reaction' AS kind
            FROM reactions r JOIN posts p ON r.post_id = p.id
            WHERE p.user_id = ? AND r.user_id != ? AND r.reaction_type IN {POSITIVE_SQL}
            UNION ALL
            SELECT c.user_id, 'comment'
            FROM comments c JOIN posts p ON c.post_id = p.id
            WHERE p.user_id = ? AND c.user_id != ?
        ) s
        JOIN users u ON u.id = s.supporter
        GROUP BY u.id
        ORDER BY reactions + comments DESC, u.username
        LIMIT ?''', (user_id, user_id, user_id, user_id, limit)) or []

    supporters = []
    for row in rows:
        person = dict(row)
        # How often this user already reacted to or commented on the supporter's posts
        person['given_back'] = query_db('''
            SELECT (SELECT COUNT(*) FROM reactions r JOIN posts p ON r.post_id = p.id
                    WHERE p.user_id = ? AND r.user_id = ?)
                 + (SELECT COUNT(*) FROM comments c JOIN posts p ON c.post_id = p.id
                    WHERE p.user_id = ? AND c.user_id = ?) AS n''',
            (person['id'], user_id, person['id'], user_id), one=True)['n']
        person['open_post'] = next_post_to_return(user_id, person['id'])
        supporters.append(person)
    return supporters


@app.route('/received')
def community_received():
    """The 'What the community gave you' page (DC30), with 'Return the favour' buttons (DC31)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in to see this page.', 'danger')
        return redirect(url_for('login'))

    totals = received_totals(user_id)
    by_type = query_db('SELECT r.reaction_type, COUNT(*) AS n FROM reactions r '
                       'JOIN posts p ON r.post_id = p.id WHERE p.user_id = ? AND r.user_id != ? '
                       'GROUP BY r.reaction_type ORDER BY n DESC', (user_id, user_id)) or []
    people = query_db('SELECT COUNT(*) AS n FROM ('
                      ' SELECT r.user_id FROM reactions r JOIN posts p ON r.post_id = p.id'
                      ' WHERE p.user_id = ? AND r.user_id != ?'
                      ' UNION'
                      ' SELECT c.user_id FROM comments c JOIN posts p ON c.post_id = p.id'
                      ' WHERE p.user_id = ? AND c.user_id != ?)', (user_id,) * 4, one=True)['n']
    followers = query_db('SELECT COUNT(*) AS n FROM follows WHERE followed_id = ?',
                         (user_id,), one=True)['n']
    best = query_db('SELECT p.id, p.content, COUNT(r.id) AS n FROM posts p '
                    'JOIN reactions r ON r.post_id = p.id AND r.user_id != p.user_id '
                    'WHERE p.user_id = ? GROUP BY p.id ORDER BY n DESC LIMIT 1', (user_id,), one=True)
    best_post = None
    if best:
        best_post = dict(best)
        best_post['content'], _ = moderate_content(best_post['content'])

    return render_template('received.html.j2', totals=totals, by_type=by_type, people=people,
                           followers=followers, best_post=best_post,
                           supporters=top_supporters(user_id),
                           reaction_emojis=REACTION_EMOJIS)


@app.route('/received/return/<int:supporter_id>')
def return_favour(supporter_id):
    """Opens the supporter's newest post that the user has not reacted to yet (DC31)."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in.', 'danger')
        return redirect(url_for('login'))
    supporter = query_db('SELECT username FROM users WHERE id = ?', (supporter_id,), one=True)
    if supporter is None:
        abort(404)
    post_id = next_post_to_return(user_id, supporter_id)
    if post_id is None:
        flash(f"You have already reacted to every post by @{supporter['username']}. Thank you!", 'info')
        return redirect(url_for('community_received'))
    flash(f"@{supporter['username']} supported your posts. This is their newest post you have not "
          f"reacted to yet. Return the favour with a reaction or a comment!", 'info')
    return redirect(url_for('post_detail', post_id=post_id))


# ----- Coding Assignment #3: Dealing with newcomers -----
# Two features, each for a design claim from the course book (Chapter 5):
#
# 1. "Newcomer journey" - Chapter 5, Design Claim 22:
#    "By using formal, sequential and collective socialization tactics, new
#     members are likely to become more committed to the community, learn how
#     to behave in it and contribute more."
#    Formal: the same official three steps for everyone, with a badge at the end.
#    Sequential: a step only counts after the step before it is done.
#    Collective: everyone who joined in the same month is one group (a cohort),
#    sees the group's progress, and the last step is to welcome another newcomer.
#    Each step also teaches one norm of the community.
#
# 2. "Welcome team and first contact" - Chapter 5, Design Claim 20:
#    "Assigning the responsibilities of having friendly interactions with
#     newcomers to particular community members increases the frequency of
#     these interactions."
#    Members can join the welcome team. Every newcomer gets one team member as
#    their first contact (as soon as the team has a member). That member sees
#    their newcomers and can send a welcome in one click. If a member leaves,
#    the newcomers they have not welcomed yet go to another member. This supports Design Claim 18 (friendly first contact
#    soon after joining makes newcomers stay).

NEWCOMER_DAYS = 30          # accounts younger than this are newcomers

# (title, what to do, the norm this step teaches)
JOURNEY_STEPS = [
    ('Introduce yourself',
     'Write a short post about you. It gets the #introduction tag, so others can find you.',
     'Here people say who they are.'),
    ('React to a post you like',
     'Give someone a reaction. Reactions are how people here say thanks.',
     'Give feedback to others.'),
    ('Welcome another newcomer',
     'Leave a kind comment on a post by another newcomer. You find them on the group page.',
     'Be kind to new people.'),
]


def create_newcomer_tables():
    """Creates the tables for Coding Assignment #3 once, if they do not exist yet."""
    with sqlite3.connect(DATABASE) as db:
        db.execute('''
            CREATE TABLE IF NOT EXISTS newcomer_journey (
                user_id   INTEGER PRIMARY KEY,
                step1_at  TIMESTAMP,     -- wrote an #introduction post
                step2_at  TIMESTAMP,     -- reacted to another member's post
                step3_at  TIMESTAMP      -- commented on another newcomer's post
            )''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS welcome_team (
                user_id    INTEGER PRIMARY KEY,
                joined_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL
            )''')
        db.execute('''
            CREATE TABLE IF NOT EXISTS first_contacts (
                newcomer_id  INTEGER PRIMARY KEY,
                helper_id    INTEGER NOT NULL,       -- the welcome team member
                assigned_at  TIMESTAMP DEFAULT CURRENT_TIMESTAMP NOT NULL,
                welcomed_at  TIMESTAMP               -- when the helper welcomed them
            )''')

create_newcomer_tables()


def user_created_at(user_id):
    """When the user made their account (None if the user does not exist)."""
    row = query_db('SELECT created_at FROM users WHERE id = ?', (user_id,), one=True)
    return row['created_at'] if row else None


def is_newcomer(user_id):
    """True if the account is younger than NEWCOMER_DAYS."""
    created = user_created_at(user_id)
    return created is not None and account_age_days(created) < NEWCOMER_DAYS


def cohort_label(cohort):
    """'2026-10' -> 'October 2026'."""
    return datetime.strptime(cohort, '%Y-%m').strftime('%B %Y')


def cohort_of(user_id):
    """The cohort is the month the account was made in, for example '2026-10'."""
    created = user_created_at(user_id)
    if created is None:
        return datetime.utcnow().strftime('%Y-%m')
    if isinstance(created, str):
        return created[:7]
    return created.strftime('%Y-%m')


# --- Feature 1: newcomer journey (Chapter 5, Design Claim 22) ---

def journey_row(user_id):
    """The user's journey record (which steps are done), or an empty dict."""
    row = query_db('SELECT * FROM newcomer_journey WHERE user_id = ?', (user_id,), one=True)
    return dict(row) if row else {}


def mark_step(user_id, step):
    """Saves that a step is done (only the first time)."""
    query_db('INSERT INTO newcomer_journey (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING',
             (user_id,), commit=True)
    # step is always 1, 2 or 3 (set by our code, never by the user)
    query_db(f'UPDATE newcomer_journey SET step{step}_at = CURRENT_TIMESTAMP '
             f'WHERE user_id = ? AND step{step}_at IS NULL', (user_id,), commit=True)


def journey_status(user_id):
    """The journey card data: the three steps (done, current or locked) and the first contact."""
    row = journey_row(user_id)
    steps = []
    for number, (title, text, norm) in enumerate(JOURNEY_STEPS, start=1):
        done = bool(row.get(f'step{number}_at'))
        unlocked = number == 1 or bool(row.get(f'step{number - 1}_at'))
        steps.append({'number': number, 'title': title, 'text': text, 'norm': norm,
                      'done': done, 'current': unlocked and not done, 'locked': not unlocked})
    cohort = cohort_of(user_id)
    cohort_size = query_db("SELECT COUNT(*) AS n FROM users WHERE strftime('%Y-%m', created_at) = ?",
                           (cohort,), one=True)['n']
    assign_first_contact(user_id)
    return {'steps': steps,
            'done_count': sum(step['done'] for step in steps),
            'complete': all(step['done'] for step in steps),
            'cohort': cohort,
            'cohort_label': cohort_label(cohort),
            'cohort_size': cohort_size,
            'contact': first_contact_of(user_id)}


def journey_after_post(user_id, content):
    """Step 1: a newcomer's post with #introduction. Returns a message or ''."""
    if '#introduction' in (content or '').lower() and is_newcomer(user_id) \
            and not journey_row(user_id).get('step1_at'):
        mark_step(user_id, 1)
        return ' Newcomer journey: step 1 done! Next: react to a post you like.'
    return ''


def journey_after_reaction(user_id, post_id, reaction_type):
    """Step 2: a positive reaction to someone else's post, after step 1."""
    if reaction_type not in POSITIVE_REACTIONS or not is_newcomer(user_id):
        return ''
    post = query_db('SELECT user_id FROM posts WHERE id = ?', (post_id,), one=True)
    row = journey_row(user_id)
    if post and post['user_id'] != user_id and row.get('step1_at') and not row.get('step2_at'):
        mark_step(user_id, 2)
        return 'Newcomer journey: step 2 done! Last step: welcome another newcomer.'
    return ''


def journey_after_comment(user_id, post_id):
    """Step 3: a comment on another newcomer's post, after step 2."""
    if not is_newcomer(user_id):
        return ''
    post = query_db('SELECT user_id FROM posts WHERE id = ?', (post_id,), one=True)
    row = journey_row(user_id)
    if post and post['user_id'] != user_id and is_newcomer(post['user_id']) \
            and row.get('step2_at') and not row.get('step3_at'):
        mark_step(user_id, 3)
        return ' Newcomer journey complete! You earned the "Settled in" badge.'
    return ''


@app.route('/introduce')
def introduce():
    """Step 1 helper page: a short introduction post with the #introduction tag."""
    if not session.get('user_id'):
        flash('You must be logged in.', 'danger')
        return redirect(url_for('login'))
    starter = f"Hi everyone! I'm {session.get('username')}. I like \n\n#introduction"
    return render_template('introduce.html.j2', starter=starter)


@app.route('/newcomers')
def newcomers():
    """The cohort page: everyone who joined in the same month, and how far they are."""
    user_id = session.get('user_id')
    cohort = cohort_of(user_id) if user_id else datetime.utcnow().strftime('%Y-%m')
    members = query_db(
        "SELECT u.id, u.username, u.created_at, j.step1_at, j.step2_at, j.step3_at, "
        "  (SELECT p.id FROM posts p WHERE p.user_id = u.id AND lower(p.content) LIKE '%#introduction%' "
        "   ORDER BY p.created_at DESC LIMIT 1) AS intro_post "
        "FROM users u LEFT JOIN newcomer_journey j ON j.user_id = u.id "
        "WHERE strftime('%Y-%m', u.created_at) = ? ORDER BY u.created_at", (cohort,)) or []
    people = []
    for row in members:
        person = dict(row)
        person['done'] = sum(1 for key in ('step1_at', 'step2_at', 'step3_at') if person[key])
        people.append(person)
    return render_template('newcomers.html.j2', people=people, cohort=cohort_label(cohort),
                           journey=journey_status(user_id) if user_id and is_newcomer(user_id) else None)


# --- Feature 2: welcome team and first contact (Chapter 5, Design Claim 20) ---

def on_welcome_team(user_id):
    """True if the user is a member of the welcome team."""
    return query_db('SELECT 1 FROM welcome_team WHERE user_id = ?', (user_id,), one=True) is not None


def assign_first_contact(newcomer_id):
    """
    Gives a newcomer one welcome team member as first contact. We pick the team
    member with the fewest newcomers, so the work is shared fairly.
    """
    if not is_newcomer(newcomer_id):
        return
    if query_db('SELECT 1 FROM first_contacts WHERE newcomer_id = ?', (newcomer_id,), one=True):
        return
    helper = query_db('''
        SELECT w.user_id FROM welcome_team w
        LEFT JOIN first_contacts f ON f.helper_id = w.user_id
        WHERE w.user_id != ?
        GROUP BY w.user_id
        ORDER BY COUNT(f.newcomer_id), w.joined_at
        LIMIT 1''', (newcomer_id,), one=True)
    if helper:
        query_db('INSERT INTO first_contacts (newcomer_id, helper_id) VALUES (?, ?)',
                 (newcomer_id, helper['user_id']), commit=True)


def first_contact_of(newcomer_id):
    """The newcomer's first contact (helper id, name and welcome time), or None."""
    row = query_db('SELECT f.helper_id, f.welcomed_at, u.username FROM first_contacts f '
                   'JOIN users u ON u.id = f.helper_id WHERE f.newcomer_id = ?', (newcomer_id,), one=True)
    return dict(row) if row else None


def note_welcome(helper_id, post_id):
    """If a first contact comments on their newcomer's post, that counts as the welcome."""
    post = query_db('SELECT user_id FROM posts WHERE id = ?', (post_id,), one=True)
    if post:
        query_db('UPDATE first_contacts SET welcomed_at = CURRENT_TIMESTAMP '
                 'WHERE newcomer_id = ? AND helper_id = ? AND welcomed_at IS NULL',
                 (post['user_id'], helper_id), commit=True)


@app.route('/welcome')
def welcome_team():
    """The welcome team page: join or leave, and the newcomers you are first contact for."""
    user_id = session.get('user_id')
    if not user_id:
        flash('You must be logged in.', 'danger')
        return redirect(url_for('login'))
    rows = query_db('''
        SELECT u.id, u.username, u.created_at, f.welcomed_at,
               (SELECT p.id FROM posts p WHERE p.user_id = u.id ORDER BY p.created_at DESC LIMIT 1) AS post_id,
               (SELECT p.content FROM posts p WHERE p.user_id = u.id ORDER BY p.created_at DESC LIMIT 1) AS post_content
        FROM first_contacts f JOIN users u ON u.id = f.newcomer_id
        WHERE f.helper_id = ?
        ORDER BY f.welcomed_at IS NOT NULL, f.assigned_at DESC''', (user_id,)) or []
    mine = []
    for row in rows:
        person = dict(row)
        if person['post_content']:
            person['post_content'], _ = moderate_content(person['post_content'])
        mine.append(person)
    team_size = query_db('SELECT COUNT(*) AS n FROM welcome_team', one=True)['n']
    return render_template('welcome.html.j2', on_team=on_welcome_team(user_id), newcomers=mine,
                           team_size=team_size)


@app.route('/welcome/join', methods=['POST'])
def join_welcome_team():
    """Adds the user to the welcome team and gives waiting newcomers a first contact."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    query_db('INSERT INTO welcome_team (user_id) VALUES (?) ON CONFLICT(user_id) DO NOTHING',
             (user_id,), commit=True)
    # newcomers who are still waiting for a first contact get one now
    waiting = query_db('SELECT id FROM users WHERE id NOT IN (SELECT newcomer_id FROM first_contacts)') or []
    for row in waiting:
        assign_first_contact(row['id'])
    flash('You joined the welcome team. Thank you!', 'success')
    return redirect(url_for('welcome_team'))


@app.route('/welcome/leave', methods=['POST'])
def leave_welcome_team():
    """Removes the user from the welcome team and hands their open newcomers to others."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    query_db('DELETE FROM welcome_team WHERE user_id = ?', (user_id,), commit=True)
    # Newcomers this member has not welcomed yet go to another team member.
    # Welcomes already sent stay, so the history is kept.
    waiting = query_db('SELECT newcomer_id FROM first_contacts WHERE helper_id = ? AND welcomed_at IS NULL',
                       (user_id,)) or []
    query_db('DELETE FROM first_contacts WHERE helper_id = ? AND welcomed_at IS NULL', (user_id,), commit=True)
    for row in waiting:
        assign_first_contact(row['newcomer_id'])
    flash('You left the welcome team.', 'info')
    return redirect(url_for('welcome_team'))


@app.route('/welcome/send/<int:newcomer_id>', methods=['POST'])
def send_welcome(newcomer_id):
    """One-click welcome: a friendly comment on the newcomer's newest post."""
    user_id = session.get('user_id')
    if not user_id:
        return redirect(url_for('login'))
    contact = query_db('SELECT 1 FROM first_contacts WHERE newcomer_id = ? AND helper_id = ?',
                       (newcomer_id, user_id), one=True)
    if not contact:
        abort(403)      # only the assigned first contact can use this button
    post = query_db('SELECT id FROM posts WHERE user_id = ? ORDER BY created_at DESC LIMIT 1',
                    (newcomer_id,), one=True)
    text = (request.form.get('content') or '').strip()
    if not post or not text:
        flash('There is no post to answer yet, or the message is empty.', 'warning')
        return redirect(url_for('welcome_team'))
    # A welcome is a comment, so it gets the same point-of-action reminder (DC20).
    reminder_page = check_before_saving(user_id, text, 'comment', post['id'],
                                        form_action=url_for('send_welcome', newcomer_id=newcomer_id),
                                        cancel_url=url_for('welcome_team'))
    if reminder_page:
        return reminder_page
    query_db('INSERT INTO comments (post_id, user_id, content) VALUES (?, ?, ?)',
             (post['id'], user_id, text), commit=True)
    note_welcome(user_id, post['id'])
    newcomer = query_db('SELECT username FROM users WHERE id = ?', (newcomer_id,), one=True)
    flash(f"Your welcome was posted on @{newcomer['username']}'s post.", 'success')
    return redirect(url_for('welcome_team'))


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

# Small words that say nothing about a topic. They are ignored by recommend().
STOP_WORDS = set('''
a about after again all also am an and any are as at be because been before being but by can could
did do does doing done for from get got had has have having he her here hers him his how i if in into
is it its just like me more most my no not now of off on once only or other our out over own same she
so some such than that the their them then there these they this those through to too under until up
very was we were what when where which while who why will with would you your yours really today
still back time make made much many even ever every feel feels felt going gonna know let lot new one
day days way thing things think want need see got take took into onto yes yeah
'''.split())

_token_cache = {'key': None, 'tokens': {}, 'idf': {}}


def post_tokens(text):
    """
    The topic words of a post: words of 3+ letters (without stop words) and hashtags.
    Hashtags written like #DIYChallenge are also split into 'diy' and 'challenge'.
    """
    words = set()
    for raw in re.findall(r"#?\w+", text or ''):
        if raw.startswith('#'):
            body = raw[1:]
            words.add('#' + body.lower())
            for part in re.findall(r'[A-Z]+(?![a-z])|[A-Z]?[a-z]+', body):
                if len(part) >= 3 and part.lower() not in STOP_WORDS:
                    words.add(part.lower())
            if body.islower() and len(body) >= 3:
                words.add(body)
        else:
            word = raw.lower()
            if len(word) >= 3 and not word.isdigit() and word not in STOP_WORDS:
                words.add(word)
    return words


def token_stats(posts):
    """
    Topic words for every post, plus how rare each word is (IDF).
    A rare word like 'diy' tells more about a person's taste than a common word.
    The result is kept until a post is added or deleted.
    """
    key = (len(posts), max((p['id'] for p in posts), default=0))
    if _token_cache['key'] != key:
        tokens = {p['id']: post_tokens(p['content']) for p in posts}
        df = collections.Counter(word for words in tokens.values() for word in words)
        total = len(posts)
        idf = {word: math.log((total + 1) / (count + 1)) + 1 for word, count in df.items()}
        _token_cache.update(key=key, tokens=tokens, idf=idf)
    return _token_cache['tokens'], _token_cache['idf']


def recommend(user_id, filter_following):
    """
    Args:
        user_id: The ID of the current user.
        filter_following: Boolean, True if we only want to see recommendations from followed users.

    Returns:
        A list of 5 recommended posts, in reverse-chronological order.

    How it works (a simple mix of "content" and "social" recommendation):
      1. Interest profile: the topic words and hashtags of the posts the user
         reacted to positively (like, love, laugh/haha, wow). Rare words count more.
      2. Every other post gets a score:
           + how well its words match the profile (a word that starts the same,
             like 'diy' and '#diysuccess', counts half)
           + a bonus if the user follows the author
           + a bonus for authors whose posts the user liked before
      3. We skip the user's own posts and posts they already reacted to.
         With filter_following, only posts from followed users are used.
      4. The 5 best posts are shown newest first.
      A user with no likes and no follows (a brand-new newcomer) gets the most
      liked posts instead, topped up with the newest posts, so the tab is not
      empty while there are posts to show. The same happens when nobody is
      logged in, and to fill the list when fewer than 5 posts match.
    Each post also gets a short 'reason', shown on the Recommended tab.

    To test whether your recommendation algorithm works, let's pretend we like the DIY topic. Here are some users that often post DIY comment and a few example posts. Make sure your account did not engage with anything else. You should test your algorithm with these and see if your recommendation algorithm picks up on your interest in DIY and starts showing related content.

    Users: @starboy99, @DancingDolphin, @blogger_bob
    Posts: 1810, 1875, 1880, 2113

    Materials:
    - https://www.nvidia.com/en-us/glossary/recommendation-system/
    - http://www.configworks.com/mz/handout_recsys_sac2010.pdf
    - https://www.researchgate.net/publication/227268858_Recommender_Systems_Handbook
    """
    posts = query_db('SELECT p.id, p.content, p.created_at, u.username, u.id AS user_id '
                     'FROM posts p JOIN users u ON u.id = p.user_id') or []
    by_id = {p['id']: p for p in posts}

    def as_result(post, reason):
        """Copies a post and adds the reason why it is recommended."""
        item = dict(post)
        item['reason'] = reason
        return item

    def popular(exclude=(), allowed_authors=None, limit=5):
        """
        Most liked posts, then the newest ones (the fallback for new users and visitors).
        The user's own posts are never suggested.
        """
        rows = query_db(f'SELECT post_id, COUNT(*) AS n FROM reactions WHERE reaction_type IN {POSITIVE_SQL} '
                        'GROUP BY post_id') or []
        likes = {row['post_id']: row['n'] for row in rows}
        ranked = sorted(posts, key=lambda p: (likes.get(p['id'], 0), str(p['created_at'])), reverse=True)
        result = []
        for post in ranked:
            if post['id'] in exclude or (user_id and post['user_id'] == user_id):
                continue
            if allowed_authors is not None and post['user_id'] not in allowed_authors:
                continue
            n = likes.get(post['id'], 0)
            result.append(as_result(post, f'Popular: {n} people liked it' if n else 'New in the community'))
            if len(result) == limit:
                break
        return result

    def newest_first(items):
        """Sorts the posts so the newest one comes first."""
        return sorted(items, key=lambda item: str(item['created_at']), reverse=True)

    if not user_id:
        return newest_first(popular())

    reacted = {row['post_id'] for row in query_db('SELECT post_id FROM reactions WHERE user_id = ?',
                                                  (user_id,)) or []}
    liked = [row['post_id'] for row in query_db(
        f'SELECT post_id FROM reactions WHERE user_id = ? AND reaction_type IN {POSITIVE_SQL}', (user_id,)) or []]
    followed = {row['followed_id'] for row in query_db('SELECT followed_id FROM follows WHERE follower_id = ?',
                                                       (user_id,)) or []}

    tokens, idf = token_stats(posts)

    # 1. Interest profile from the liked posts
    profile = collections.Counter()
    liked_authors = collections.Counter()
    for post_id in liked:
        if post_id in by_id:
            liked_authors[by_id[post_id]['user_id']] += 1
            for word in tokens.get(post_id, ()):
                profile[word] += idf.get(word, 1.0)
    short_words = [w.lstrip('#') for w in profile if 3 <= len(w.lstrip('#')) <= 6]

    if not profile and not followed:
        # Cold start: nothing to learn from yet
        allowed = followed if filter_following else None
        return newest_first(popular(exclude=reacted, allowed_authors=allowed))

    # 2. Score every candidate post
    scored = []
    for post in posts:
        if post['user_id'] == user_id or post['id'] in reacted:
            continue
        if filter_following and post['user_id'] not in followed:
            continue
        words = tokens.get(post['id'], set())
        matches = {w: profile[w] for w in words if w in profile}
        content = sum(matches.values())
        for word in words - set(matches):
            plain = word.lstrip('#')
            starts = [w for w in short_words if plain != w and plain.startswith(w)]
            if starts:
                best = max(starts, key=lambda w: profile.get(w, 0) + profile.get('#' + w, 0))
                content += 0.5 * max(profile.get(best, 0), profile.get('#' + best, 0))
                matches.setdefault(best, 0)
        social = 1.5 if post['user_id'] in followed else 0.0
        affinity = 1.0 * liked_authors[post['user_id']]
        score = content + social + affinity
        if score <= 0:
            continue
        if matches:
            top_word = max(matches, key=lambda w: (profile.get(w, 0), w.startswith('#')))
            label = top_word if top_word.startswith('#') else f"'{top_word}'"
            reason = f'Because you liked posts about {label}'
        elif social:
            reason = f"From @{post['username']}, who you follow"
        else:
            reason = f"You liked posts by @{post['username']} before"
        scored.append((score, str(post['created_at']), as_result(post, reason)))

    # 3. The 5 best, shown newest first. Fill with popular posts if fewer than 5.
    scored.sort(key=lambda item: (item[0], item[1]), reverse=True)
    best = [item[2] for item in scored[:5]]
    if len(best) < 5:
        taken = reacted | {item['id'] for item in best}
        allowed = followed if filter_following else None
        best += popular(exclude=taken, allowed_authors=allowed, limit=5 - len(best))
    return newest_first(best)

if __name__ == '__main__':
    app.run(debug=True, port=8080)

