"""Fuller default text for the FAQ, Safety, Privacy and Terms pages, plus starter FAQ items.

Only replaces a page body that is still the short demo text, so edits made in the admin are kept.
Everything stays editable in Admin → Topics (pages) and Admin → FAQ items.
"""

from django.db import migrations

PAGES = {
    "faq": (
        "Answers to the questions we get most often.",
        "Can't find your answer? Use the search above, or [contact us](/info/contact) – we reply within one working day.",
    ),
    "safety": (
        "How to stay safe, and how to report an ad or a person.",
        """## Our promise
Every ad is checked by a moderator before it goes live, and every advertiser must be 18 or older. We remove ads that break our rules, and we work with the police when someone is at risk.

## Meeting someone safely
- Meet in a place you know, and tell a friend where you are going and when you expect to be back.
- Agree on everything – time, place, services and price – in writing in the chat **before** you meet.
- Trust your feeling. If something seems wrong, leave.
- Never share your home address, workplace or ID before you trust the person.

## Money and scams
- Never pay in advance to someone you have not met, and never send gift cards, crypto or bank transfers to "unlock" a profile.
- We never ask for your password, card number or a code sent to your phone.
- Ads that ask for money up front are against our rules – please report them.

## How to report
- **An ad:** open the ad and press **Report**. Choose a reason and add details.
- **A person in chat:** open the chat, press the info button and choose **Report**. You can block them at the same time.
- They are never told who reported them. Reports about minors or human trafficking are reviewed first.

## In danger?
Call the police on **100** (Nepal). In an emergency always contact the police first – then tell us so we can remove the ad.

## Help for advertisers
If you are being forced, threatened or are not paid what was agreed, you can contact us at any time. We will remove your ad at once if you ask, and we never share your details with anyone except the police when the law requires it.""",
    ),
    "privacy": (
        "What we collect, why, and how you stay in control.",
        """## Summary
We collect as little as possible, we never sell your data, and you can delete your account at any time.

## What we collect
- **Account:** your e-mail address, a password (stored encrypted) and your settings.
- **Ads:** the text, pictures and contact details you choose to show. Location data is removed from pictures when you upload them.
- **Messages:** chats between you and other users, so they can be delivered and so moderators can look into reports.
- **ID checks:** documents for age verification are only seen by moderators and are deleted once the check is done.
- **Technical data:** basic logs (IP address, browser) to keep the site secure and stop abuse.

## Why we use it
- To run your account, show your ads and deliver messages.
- To keep the site safe – checking ads, handling reports and stopping fraud.
- To send e-mails you asked for, like confirmations and message alerts (you can turn alerts off in Settings).

## Cookies
We use necessary cookies to keep you logged in and remember your choices (like dark mode and your country). Other cookies are only used if you allow them – change this any time under **Cookie settings** at the bottom of every page.

## Who we share with
Nobody, except payment providers to handle a payment and the police when the law requires it.

## Your rights
You can see, correct, download or delete your data. Delete your account in **Settings**, or [contact us](/info/contact) and choose "Privacy / delete my data".

## How long we keep data
Ads and messages are deleted when you delete your account. Logs are kept for up to 90 days.""",
    ),
    "terms": (
        "The rules for using the site. By using it you agree to them.",
        """## 1. Who can use the site
You must be at least 18 years old. Advertisers must verify their age before an ad is shown.

## 2. Your account
Keep your password private. You are responsible for what happens on your account. One person, one account.

## 3. Ads
- Ads must be truthful and show only you, with your permission.
- No minors, no coercion, no illegal services, and no asking for money in advance.
- We may refuse, pause or remove any ad that breaks these rules, without refund if the rules were broken on purpose.

## 4. Messages
Be respectful. Harassment, threats, spam and scams lead to a block and may be reported to the police.

## 5. Payments and coins
Prices are shown before you pay. Plans start when the ad is activated. Coins have no cash value and cannot be exchanged for money.

## 6. Content and rights
You keep the rights to what you upload, and you allow us to show it on the site while your ad is live.

## 7. Our responsibility
We are a listings site. We do not take part in meetings between users and are not responsible for what users agree with each other.

## 8. Changes
We may change these terms. We tell you about important changes by e-mail or on the site before they apply.

## 9. Contact
Questions about these terms? [Contact us](/info/contact).""",
    ),
}

FAQ = [
    ("general", "What is this site?", "A listings site where adults (18+) advertise massage, wellness and companionship. Every ad is checked by a moderator before it goes live."),
    ("general", "Is it free to browse?", "Yes. Browsing, searching and saving favourites are free. You only need an account to send messages."),
    ("general", "Which areas do you cover?", "All of Nepal – Kathmandu, Pokhara, Chitwan and more. Pick a country on the map or use **Change country** on the front page."),
    ("account", "How do I create an account?", "Press **Create account**, enter your e-mail and a password, then confirm your e-mail with the link we send you."),
    ("account", "I forgot my password", "Press **Log in** and then **Forgot password**. We send a link to reset it."),
    ("account", "How do I delete my account?", "Go to **Settings** → **Delete account**. Your ads, pictures and messages are removed."),
    ("ads", "How do I create an ad?", "Sign up as an advertiser, verify your age, then press **Create ad**. Fill in the steps and choose **Activate ad** at the end – or **Save as draft** to finish later."),
    ("ads", "How long is an ad live?", "A free ad runs for the number of days set by the site. Paid plans run longer and are shown higher in the lists. You can extend an ad at any time."),
    ("ads", "Why was my ad not approved?", "The reason is shown on the ad in **My ads**. Fix it and send it again. Common reasons: pictures that are not of you, missing contact details, or asking for money in advance."),
    ("ads", "How many pictures can I add?", "Up to 12. The first picture is the front picture – drag the pictures to change the order."),
    ("payments", "How can I pay?", "By card, or with coins. Prices are shown before you pay."),
    ("payments", "What are coins?", "Coins are credits you buy once and use for plans and extras like **Bump** (moving your ad back to the top). Coins have no cash value."),
    ("payments", "Can I get a refund?", "If something went wrong with a payment, [contact us](/info/contact) and choose **Payments and coins**."),
    ("safety", "How do I report an ad or a person?", "Press **Report** on the ad, or in a chat open the info panel and choose **Report**. They are never told who reported them."),
    ("safety", "Who can see my e-mail and phone number?", "Only what you choose to show on your ad. Your login e-mail is never shown."),
    ("safety", "I am in danger – what do I do?", "Call the police on **100** first. Then tell us and we remove the ad straight away."),
]


def forward(apps, schema_editor):
    Topic = apps.get_model("core", "Topic")
    FaqItem = apps.get_model("core", "FaqItem")
    for slug, (summary, body) in PAGES.items():
        t = Topic.objects.filter(kind="page", slug=slug).first()
        if t is None:
            continue
        if not t.summary or t.summary.lstrip().startswith("#"):
            t.summary = summary
        if len(t.body) < 400:  # still the short demo text
            t.body = body
        t.save()
    if not FaqItem.objects.exists():
        FaqItem.objects.bulk_create(
            [FaqItem(category=c, question=q, answer=a, order=i) for i, (c, q, a) in enumerate(FAQ)]
        )


class Migration(migrations.Migration):
    dependencies = [("core", "0055_faq_and_page_pdf")]
    operations = [migrations.RunPython(forward, migrations.RunPython.noop)]
