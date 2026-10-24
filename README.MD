# Mini Social

This is a mock social media platform created for the Social Computing course at the University of Oulu by Dr. Aku Visuri and Daniel Szabo.

## My coursework (Syed Amin Al Mahim, Social Computing 2026-2027)

All my coding assignments are in this fork. In `app.py`, search for "Coding Assignment #1", "Coding Assignment #2" or "Coding Assignment #3" to find each part. New database tables are created automatically when the app starts.

### Coding Assignment 1: Contribution and Commitment

| Feature | Design claim | Where in the code |
|---|---|---|
| **Waiting for you**: posts from people you follow that still need your reaction or comment, with a counter | Chapter 2, Design Claim 1 | `app.py`: `waiting_count()`, `waiting_posts()`, the `waiting` sort in `feed()` |
| **Weekly goal**: a suggested goal that is a bit hard, progress bars, and feedback after every post or comment | Chapter 2, Design Claims 13 and 15 | `app.py`: `suggest_goal()`, `goal_status()`, `goal_feedback()`, `weekly_goal()` (`/goals`); `templates/goals.html.j2` |
| **What the community gave you**: what you received from others, with a **Return the favour** button for each person | Chapter 3, Design Claims 30 and 31 | `app.py`: `received_totals()`, `top_supporters()`, `community_received()` (`/received`), `return_favour()`; `templates/received.html.j2` |

The three cards are at the top of the feed (`templates/feed.html.j2`). The menu links are in `templates/base.html.j2`.

**Try it:** log in, follow a few people, and look at the three cards at the top of the feed. The user menu has "Weekly goal" and "What I received".

### Coding Assignment 2: Moderation

| Part | Where in the code |
|---|---|
| `moderate_content()` (Assignment 2.1), Rules 1.1 and 1.2 | End of `app.py`. The helper `analyse_content()` does the checks and remembers which rules were broken. |
| `user_risk_analysis()` (Assignment 2.2), Rule 2.2 | End of `app.py` |
| **Reminder before posting** (Chapter 4, Design Claim 20) | `app.py`: `check_before_saving()`, `explain_moderation()`, `cancel_reminder()`, `reminder_statistics()`, called from `add_post()` and `add_comment()`; reminder page `templates/review_content.html.j2`; numbers for the admin in `templates/admin.html.j2` |

**Try it:** sign up and write a post or comment with `tier3badword`, `tier2badword` or a link. Before anything is saved, a page shows which rule the text breaks and how others would see it. You can edit it, post it anyway, or cancel. Log in as `admin` / `admin` and open `/admin` to see the reminder numbers and the user risk scores.

### Coding Assignment 3: Dealing with newcomers

| Part | Design claim | Where in the code |
|---|---|---|
| `recommend()` (Assignment 3.1): 5 posts based on the posts you reacted to positively and the people you follow, newest first, each with a short reason | | End of `app.py`: `recommend()`, `post_tokens()`, `token_stats()`; the reason line on the Recommended tab in `templates/feed.html.j2` |
| **Newcomer journey**: the same 3 steps, in order, for everyone who joined in the same month, with a group page and a "Settled in" badge | Chapter 5, Design Claim 22 | `app.py`: `journey_status()`, `mark_step()`, `journey_after_post()`, `journey_after_reaction()`, `journey_after_comment()`, `introduce()` (`/introduce`), `newcomers()` (`/newcomers`); `templates/introduce.html.j2`, `templates/newcomers.html.j2`; the card in `templates/feed.html.j2` |
| **Welcome team and first contact**: every newcomer gets one welcome team member, who can welcome them in one click | Chapter 5, Design Claim 20 (supports Design Claim 18) | `app.py`: `assign_first_contact()`, `first_contact_of()`, `note_welcome()`, `welcome_team()` (`/welcome`), `send_welcome()`; `templates/welcome.html.j2` |

**Try it:** to test `recommend()`, make a new account, follow @starboy99, @DancingDolphin and @blogger_bob, react to posts 1810, 1875, 1880 and 2113, and open the Recommended tab: it shows DIY posts. For the newcomer features, let an existing user join the welcome team (user menu, "Welcome team"), then sign up a new account and follow the steps on the journey card.

The codebase contains three missing implementations, which you find [at end of app.py](https://github.com/Crowd-Computing-Oulu/mini_social_exercise/blob/44117e36e609b52c3452226797a0fd3fb5ff5258/app.py#L886). These are part of the coding assignments #1 and #2.

The repository contains a ready database, you do not have to create your own data.

## Usage (for students)

1. Create a [fork](https://docs.github.com/en/pull-requests/collaborating-with-pull-requests/working-with-forks/fork-a-repo) of this repository
2. Clone your forked repository onto your computer
3. Make sure you have Python 3.11 installed
4. Install dependencies by running `pip install -r requirements.txt`
5. Start the server by running `pyhon ./app.py`
6. The web application will be available at http://127.0.0.1:8080. 
7. The admin interface is at http://127.0.0.1:8080/admin, available only to the Admin user (username: admin, pw: admin)
8. Make sure your fork is public, and add its URL to your coursework assignments.

### Docker

If you prefer to run Mini-Social as a docker container, you can build the image and run it as follows:

1. Build image with `docker build --tag mini-social .`
2. Run image with the following command, where 8000 is the port you will access the application at `http://127.0.0.1:8000`

```
docker run -d -p 8000:5000 \
    --restart always \
    -v $(pwd)/database.sqlite:/python-docker/database.sqlite \
    mini-social
```
 
