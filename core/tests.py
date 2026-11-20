import io
from io import BytesIO

from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone
from PIL import Image
from rest_framework.test import APITestCase

from .models import Ad, Report, User


def make_image(name="p.jpg"):
    buf = BytesIO()
    Image.new("RGB", (40, 60), "purple").save(buf, "JPEG")
    return SimpleUploadedFile(name, buf.getvalue(), content_type="image/jpeg")




def review_on():
    """Site settings → Review new ads (off by default: ads go live at once)."""
    from .models import SiteSettings
    site = SiteSettings.load()
    site.review_ads = True
    site.save()

class Base(APITestCase):
    def setUp(self):
        self.adv = User.objects.create_user("adv@test.dk", "pw-Strong-123", is_advertiser=True, age_verified=True)
        # VIP visitor: messages, comments and reviews need VIP (VipTests checks the Free side).
        self.visitor = User.objects.create_user("v@test.dk", "pw-Strong-123",
                                                vip_until=timezone.now() + timedelta(days=30))

    def make_ad(self, **kw):
        defaults = dict(owner=self.adv, title="Test ad", description="x", category="massage",
                        gender="female", region="kathmandu", age=30, audiences=["men"])
        defaults.update(kw)
        ad = Ad.objects.create(**defaults)
        return ad


class PublicAdsTests(Base):
    def test_only_live_ads_are_listed(self):
        live = self.make_ad(title="Live")
        live.approve()
        self.make_ad(title="Draft")
        res = self.client.get("/api/ads")
        self.assertEqual([a["title"] for a in res.data["results"]], ["Live"])

    def test_filters(self):
        a = self.make_ad(title="CPH massage"); a.approve()
        b = self.make_ad(title="Aarhus escort", region="pokhara", category="escort", audiences=["women"]); b.approve()
        titles = lambda q: [x["title"] for x in self.client.get(f"/api/ads?{q}").data["results"]]
        self.assertEqual(titles("region=pokhara"), ["Aarhus escort"])
        self.assertEqual(titles("category=massage,clinic"), ["CPH massage"])
        self.assertEqual(titles("audience=men"), ["CPH massage"])  # "women" must not match "men"
        self.assertEqual(titles("q=escort"), ["Aarhus escort"])

    def test_heart_toggle_requires_login(self):
        ad = self.make_ad(); ad.approve()
        self.assertEqual(self.client.post(f"/api/ads/{ad.id}/heart").status_code, 403)
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(f"/api/ads/{ad.id}/heart").data["heart_count"], 1)
        self.assertEqual(self.client.post(f"/api/ads/{ad.id}/heart").data["heart_count"], 1)  # idempotent
        self.assertEqual(self.client.delete(f"/api/ads/{ad.id}/heart").data["heart_count"], 0)

    def test_anonymous_report(self):
        ad = self.make_ad(); ad.approve()
        from django.core import mail
        body = {"reason": "underage", "message": "looks young", "reporter_name": "Ann", "contact_email": "a@x.dk", "good_faith": True}
        self.assertEqual(self.client.post(f"/api/ads/{ad.id}/report", body, format="json").status_code, 400)  # < 50 chars
        body["message"] = "The person in the pictures looks much younger than eighteen years old to me."
        res = self.client.post(f"/api/ads/{ad.id}/report", {**body, "picture_ids": [999]}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        r = Report.objects.get()
        self.assertEqual((r.status, r.picture_ids), ("draft", []))  # unknown picture ids dropped
        token = mail.outbox[-1].body.split("token=")[1].split()[0]
        self.assertEqual(self.client.post("/api/reports/confirm", {"token": token}, format="json").status_code, 200)
        r.refresh_from_db()
        self.assertEqual(r.status, "open")
        self.assertTrue(r.is_urgent)


class AdvertiserFlowTests(Base):
    def test_create_upload_submit(self):
        review_on()  # this test covers the moderator flow
        self.client.force_authenticate(self.adv)
        res = self.client.post("/api/my-ads", {
            "title": "My ad", "description": "Hello", "category": "massage", "gender": "female",
            "region": "dharan", "age": 25, "audiences": ["men", "couples"],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        ad_id = res.data["id"]
        self.assertEqual(res.data["status"], "draft")

        res = self.client.post(f"/api/my-ads/{ad_id}/pictures", {"files": [make_image()]}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["pictures"][0]["url"].endswith(".webp"))

        res = self.client.post(f"/api/my-ads/{ad_id}/submit")
        self.assertEqual(res.data["status"], "pending")

    def test_contact_options_captions_and_front_picture(self):
        self.client.force_authenticate(self.adv)
        ad_id = self.client.post("/api/my-ads", {
            "title": "My ad", "description": "Hello", "category": "massage", "gender": "female", "region": "dharan",
            "age": 25, "phone": "+45 12 34 56 78", "show_sms": False, "contact_email": "me@example.com",
        }, format="json").data["id"]
        pics = self.client.post(f"/api/my-ads/{ad_id}/pictures", {"files": [make_image(), make_image("b.jpg")]}, format="multipart").data["pictures"]
        first, second = pics[0]["id"], pics[1]["id"]
        res = self.client.patch(f"/api/my-ads/{ad_id}/pictures/{second}", {"caption": "  In my studio  "}, format="json")
        self.assertEqual(res.data["pictures"][1]["caption"], "In my studio")
        res = self.client.post(f"/api/my-ads/{ad_id}/pictures/order", {"ids": [second, first]}, format="json")
        self.assertEqual([p["id"] for p in res.data["pictures"]], [second, first])  # "make front picture"
        self.assertEqual(self.client.post(f"/api/my-ads/{ad_id}/pictures/order", {"ids": "x"}, format="json").status_code, 400)
        from .models import Ad
        Ad.objects.get(pk=ad_id).approve()
        d = self.client.get(f"/api/ads/{ad_id}").data
        self.assertEqual((d["show_call"], d["show_sms"], d["contact_email"]), (True, False, "me@example.com"))
        self.assertEqual(d["pictures"][0]["caption"], "In my studio")

    def test_under_18_rejected(self):
        self.client.force_authenticate(self.adv)
        res = self.client.post("/api/my-ads", {
            "title": "x", "description": "x", "category": "massage", "gender": "female",
            "region": "dharan", "age": 17,
        }, format="json")
        self.assertEqual(res.status_code, 400)

    def test_visitor_cannot_create_ads(self):
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.get("/api/my-ads").status_code, 403)

    def test_cannot_touch_other_users_ads(self):
        other = User.objects.create_user("o@test.dk", "pw-Strong-123", is_advertiser=True)
        ad = self.make_ad(owner=other)
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.patch(f"/api/my-ads/{ad.id}", {"title": "hacked"}, format="json").status_code, 404)


class AuthTests(Base):
    def test_signup_requires_adult_and_terms(self):
        res = self.client.post("/api/auth/signup", {
            "email": "new@test.dk", "password": "pw-Strong-123", "confirm_adult": False, "accept_terms": True,
        }, format="json")
        self.assertEqual(res.status_code, 400)
        res = self.client.post("/api/auth/signup", {
            "email": "new@test.dk", "password": "pw-Strong-123", "confirm_adult": True, "accept_terms": True,
        }, format="json")
        self.assertEqual(res.status_code, 201)
        self.assertEqual(self.client.get("/api/auth/me").data["email"], "new@test.dk")

    def test_login_logout(self):
        res = self.client.post("/api/auth/login", {"email": "V@test.dk", "password": "pw-Strong-123"}, format="json")
        self.assertEqual(res.status_code, 200)
        self.client.post("/api/auth/logout")
        self.assertIsNone(self.client.get("/api/auth/me").data)


# ---------------------------------------------------------------------------
# Payments
# ---------------------------------------------------------------------------

import hashlib
from datetime import timedelta
from unittest import mock

from django.test import override_settings

from . import payments
from .models import Order, Product


@override_settings(PAYMENTWINGS_PUBLIC_KEY="", PAYMENTWINGS_PRIVATE_KEY="", PAYMENTS_ALLOW_FAKE=True)
class FakePaymentFlowTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make_ad()
        self.ad.approve()
        self.client.force_authenticate(self.adv)

    def test_checkout_and_pay_applies_tier_once(self):
        res = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "vip-7d"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        oid = res.data["order_id"]
        self.assertIn(f"/payments/fake/{oid}", res.data["payment_url"])

        res = self.client.post(f"/api/payments/fake/{oid}", {"result": "success"}, format="json")
        self.assertEqual(res.data["status"], "paid")
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.effective_tier, "vip")
        first_until = self.ad.tier_until
        self.assertAlmostEqual((first_until - timezone.now()).days, 6, delta=1)

        # Replaying the same success must not extend again.
        self.client.post(f"/api/payments/fake/{oid}", {"result": "success"}, format="json")
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.tier_until, first_until)

        # Public API shows the paid tier.
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["tier"], "vip")

    def test_buying_same_tier_again_extends(self):
        for _ in range(2):
            oid = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d"}, format="json").data["order_id"]
            self.client.post(f"/api/payments/fake/{oid}", {"result": "success"}, format="json")
        self.ad.refresh_from_db()
        self.assertGreaterEqual((self.ad.tier_until - timezone.now()).days, 59)

    def test_failed_payment(self):
        oid = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d"}, format="json").data["order_id"]
        res = self.client.post(f"/api/payments/fake/{oid}", {"result": "fail"}, format="json")
        self.assertEqual(res.data["status"], "failed")
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.effective_tier, "free")

    def test_free_renew_is_instant_and_only_near_the_end(self):
        self.ad.expires_at = timezone.now() + timedelta(days=20)
        self.ad.save()
        checkout = lambda: self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "free-7d"}, format="json")
        self.assertEqual(checkout().status_code, 400)  # more than 7 days left
        self.ad.expires_at = before = timezone.now() + timedelta(days=3)
        self.ad.save()
        res = checkout()
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(self.client.get(f"/api/payments/{res.data['order_id']}").data["status"], "paid")  # no payment window
        self.ad.refresh_from_db()
        self.assertEqual((self.ad.expires_at - before).days, 7)
        self.assertEqual(self.ad.effective_tier, "free")

    def test_standard_is_a_paid_tier(self):
        oid = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "standard-7d"}, format="json").data["order_id"]
        self.client.post(f"/api/payments/fake/{oid}", {"result": "success"}, format="json")
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.effective_tier, "standard")

    def test_cannot_pay_for_or_read_others(self):
        other = User.objects.create_user("o2@test.dk", "pw-Strong-123", is_advertiser=True)
        other_ad = self.make_ad(owner=other)
        res = self.client.post(f"/api/my-ads/{other_ad.id}/checkout", {"product": "premium-30d"}, format="json")
        self.assertEqual(res.status_code, 404)
        oid = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d"}, format="json").data["order_id"]
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/payments/{oid}").status_code, 404)

    def test_expired_tier_is_free(self):
        self.ad.tier, self.ad.tier_until = "vip", timezone.now() - timedelta(minutes=1)
        self.ad.save()
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["tier"], "free")

    @override_settings(PAYMENTS_ALLOW_FAKE=False)
    def test_no_gateway_configured(self):
        res = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d"}, format="json")
        self.assertEqual(res.status_code, 400)


@override_settings(PAYMENTWINGS_PUBLIC_KEY="pub_test", PAYMENTWINGS_PRIVATE_KEY="sec_test", PAYMENTWINGS_WEBHOOK_KEY="hook-key",
                   PUBLIC_SITE_URL="https://velora.test", PUBLIC_API_URL="https://velora.test")
class PaymentWingsTests(Base):
    TXN = "TRN_ABC123"

    def setUp(self):
        super().setUp()
        self.ad = self.make_ad()
        self.ad.approve()
        self.client.force_authenticate(self.adv)

    def checkout(self, code="premium-30d"):
        calls = []

        def fake_request(gw, method, path, body=None, auth=True):
            calls.append((method, path, body))
            return {"ok": True, "data": {"object": "transaction", "transaction_id": self.TXN,
                                         "url": f"https://pay.paymentwings.com/checkout/{self.TXN}", "mode": "sandbox"}}

        with mock.patch.object(payments.PaymentWingsGateway, "_request", fake_request):
            res = self.client.post(f"/api/my-ads/{self.ad.id}/checkout", {"product": code}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return Order.objects.get(order_id=res.data["order_id"]), calls, res.data

    def test_creates_payment(self):
        order, calls, data = self.checkout()
        self.assertEqual(data["payment_url"], f"https://pay.paymentwings.com/checkout/{self.TXN}")
        self.assertEqual(order.gateway, "paymentwings")
        self.assertEqual(order.gateway_payment_id, self.TXN)
        method, path, body = calls[0]
        self.assertEqual((method, path), ("POST", "/api/v2/payment/create"))
        self.assertEqual(body["amount"], Product.objects.get(code="premium-30d").price / 100)
        self.assertEqual(body["currency"], "USD")
        self.assertEqual(body["metadata"], order.order_id)
        self.assertEqual(body["webhook_url"], "https://velora.test/api/payments/paymentwings/webhook")
        self.assertIn(order.order_id, body["success_url"])

    def test_auth_header(self):
        gw = payments.get_gateway()
        seen = {}

        class Res:
            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def read(self):
                return b'{"ok": true}'

        def fake_open(req, timeout, **kw):
            seen["auth"] = req.get_header("Authorization")
            return Res()

        with mock.patch("urllib.request.urlopen", fake_open):
            gw._request("GET", "/x")
        self.assertEqual(seen["auth"], "Bearer pub_test:sec_test")

    def webhook(self, order, status="COMPLETED", key="hook-key", txn=None, metadata=None):
        txn = txn or self.TXN
        ts = "1759532400"
        sig = hashlib.md5(f"{txn}|{ts}|{key}".encode()).hexdigest()
        payload = {"status": status, "txn": txn, "timestamp": int(ts), "signature": sig,
                   "metadata": metadata if metadata is not None else order.order_id}
        self.client.force_authenticate(None)
        return self.client.post("/api/payments/paymentwings/webhook", payload, format="json")

    def test_valid_webhook_marks_paid(self):
        order, _, _ = self.checkout()
        self.assertEqual(self.webhook(order).status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "paid")
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.effective_tier, "premium")
        self.assertEqual(self.webhook(order).status_code, 200)  # repeated webhook: applied once
        self.assertEqual(Order.objects.get(pk=order.pk).status, "paid")

    def test_bad_signature_rejected(self):
        order, _, _ = self.checkout()
        self.assertEqual(self.webhook(order, key="wrong").status_code, 400)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    def test_not_completed_does_nothing(self):
        order, _, _ = self.checkout()
        self.webhook(order, status="PENDING")
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    def test_unknown_txn_or_wrong_metadata_ignored(self):
        order, _, _ = self.checkout()
        self.assertEqual(self.webhook(order, txn="TRN_OTHER").status_code, 200)
        self.assertEqual(self.webhook(order, metadata="VLSOMETHINGELSE").status_code, 200)
        order.refresh_from_db()
        self.assertEqual(order.status, "pending")

    def lookup(self, order, amount=None, currency="USD", status="COMPLETED"):
        amount = order.amount / 100 if amount is None else amount
        return {"status": "success", "data": {"tid": self.TXN, "status": status, "amount": amount, "currency": currency}}

    def test_return_page_lookup_marks_paid(self):
        order, _, _ = self.checkout()
        with mock.patch.object(payments.PaymentWingsGateway, "_request", return_value=self.lookup(order)):
            res = self.client.get(f"/api/payments/{order.order_id}")
        self.assertEqual(res.data["status"], "paid")

    def test_lookup_underpaid_or_without_amount_not_applied(self):
        order, _, _ = self.checkout()
        with mock.patch.object(payments.PaymentWingsGateway, "_request", return_value=self.lookup(order, amount=1)):
            self.client.get(f"/api/payments/{order.order_id}")
        bare = {"status": "success", "data": {"tid": self.TXN, "status": "COMPLETED"}}
        with mock.patch.object(payments.PaymentWingsGateway, "_request", return_value=bare):
            res = self.client.get(f"/api/payments/{order.order_id}")
        self.assertEqual(res.data["status"], "pending")


class PictureTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make_ad(region="pokhara")
        self.ad.approve()
        self.client.force_authenticate(self.adv)
        self.client.post(f"/api/my-ads/{self.ad.id}/pictures", {"files": [make_image(), make_image("b.jpg")]}, format="multipart")
        self.ad.refresh_from_db()
        if self.ad.status != Ad.Status.ACTIVE:
            self.ad.approve()
        self.client.force_authenticate(None)
        hidden = self.make_ad(title="Draft")  # not live: its pictures must not show
        AdPicture = self.ad.pictures.model
        AdPicture.objects.create(ad=hidden, image="ads/x.webp")

    def test_newest_pictures_lists_live_only_and_filters(self):
        res = self.client.get("/api/ads/new-pictures")
        self.assertEqual(res.status_code, 200)
        self.assertEqual(len(res.data["results"]), 2)
        self.assertEqual(res.data["ad_count"], 1)
        self.assertTrue(all(str(p["ad_id"]) == str(self.ad.id) for p in res.data["results"]))
        self.assertEqual(len(self.client.get("/api/ads/new-pictures?region=funen").data["results"]), 0)

    def test_picture_heart_toggle(self):
        pic = self.ad.pictures.first()
        self.assertEqual(self.client.post(f"/api/pictures/{pic.id}/heart").status_code, 403)
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(f"/api/pictures/{pic.id}/heart").data, {"hearted": True, "heart_count": 1})
        self.assertEqual(self.client.post(f"/api/pictures/{pic.id}/heart").data["heart_count"], 1)
        self.assertEqual(self.client.get("/api/hearts/me/pictures").data["picture_ids"], [pic.id])
        self.assertEqual(self.client.delete(f"/api/pictures/{pic.id}/heart").data, {"hearted": False, "heart_count": 0})


class DictionaryTests(APITestCase):
    def setUp(self):
        from .models import Topic
        Topic.objects.create(kind="dictionary", slug="incall", title="Incall", aliases="In-call", body="x")
        Topic.objects.create(kind="dictionary", slug="24-7", title="24/7", body="x")
        Topic.objects.create(kind="dictionary", slug="hidden", title="Hidden", body="x", is_published=False)

    def test_index_lists_terms_and_aliases_by_letter(self):
        d = self.client.get("/api/dictionary").data
        # "#" (non-letters) first, then A–Z; the alias links to its main entry; unpublished is hidden.
        self.assertEqual(
            [(t["term"], t["letter"], t["slug"]) for t in d["terms"]],
            [("24/7", "#", "24-7"), ("In-call", "I", "incall"), ("Incall", "I", "incall")],
        )
        self.assertEqual(d["entry_count"], 2)

    def test_entry_has_aliases(self):
        self.assertEqual(self.client.get("/api/topics/dictionary/incall").data["aliases"], ["In-call"])


class GuideTests(Base):
    def setUp(self):
        super().setUp()
        from .models import Tag, Topic
        self.article = Topic.objects.create(kind="article", slug="a1", title="A1", body="x")
        self.tag = Tag.objects.create(name="Hot stone")
        Tag.objects.create(name="Unused")
        ad = self.make_ad()
        ad.approve()
        ad.tags.add(self.tag)

    def test_tags_have_colours_and_counts(self):
        tags = {t["slug"]: t for t in self.client.get("/api/tags").data}
        self.assertEqual(set(tags), {"hot-stone", "unused"})  # all tags, A–Z
        self.assertTrue(tags["hot-stone"]["color"].startswith("#"))
        self.assertEqual(tags["hot-stone"]["ad_count"], 1)
        self.assertEqual([t["slug"] for t in self.client.get("/api/tags?used=1").data], ["hot-stone"])
        self.assertEqual(self.client.get("/api/tags/hot-stone").data["name"], "Hot stone")

    def test_topic_heart(self):
        url = f"/api/topics/{self.article.id}/heart"
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(url).data, {"hearted": True, "heart_count": 1})
        self.assertEqual(self.client.get("/api/hearts/me/topics").data["topic_ids"], [self.article.id])
        self.assertEqual(self.client.delete(url).data["heart_count"], 0)



class SiteAndPlanTests(APITestCase):
    def test_site_settings_singleton_and_api(self):
        from .models import SiteSettings
        st = SiteSettings.load()
        st.site_name = "Nordic Calm"
        st.save()
        SiteSettings(site_name="Second").save()  # still one row
        self.assertEqual(SiteSettings.objects.count(), 1)
        d = self.client.get("/api/site").data
        self.assertEqual(d["site_name"], "Second")
        self.assertIn("version", d)
        self.assertEqual(self.client.get("/api/meta").data["site_name"], "Second")

    def test_plans_have_six_durations(self):
        d = self.client.get("/api/plans").data
        self.assertEqual([p["tier"] for p in d], ["free", "standard", "premium", "vip"])
        self.assertEqual([x["days"] for x in d[1]["prices"]], [1, 7, 30, 90, 180, 365])
        self.assertEqual(d[1]["prices"][2]["duration_label"], "1 month")

    def test_zero_free_days_keeps_unpaid_ad_offline(self):
        from .models import SiteSettings
        st = SiteSettings.load()
        st.free_ad_days = 0
        st.save()
        u = User.objects.create_user("z@test.dk", "pw-Strong-123", is_advertiser=True, age_verified=True)
        ad = Ad.objects.create(owner=u, title="t", description="x", category="massage", gender="female",
                               region="dharan", age=30)
        ad.approve()
        self.assertFalse(ad.is_live)


class AccountSettingsTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.visitor)

    def test_me_has_settings_and_customer_no(self):
        d = self.client.get("/api/auth/me").data
        self.assertEqual(d["customer_no"], f"C-{self.visitor.pk:07d}")
        self.assertFalse(d["email_confirmed"])
        d = self.client.patch("/api/auth/me", {"sender_name": "neutral", "language": "da", "theme": "dark",
                                               "notify_offers": True}, format="json").data
        self.assertEqual((d["sender_name"], d["language"], d["theme"], d["notify_offers"]),
                         ("neutral", "da", "dark", True))

    def test_phone_change_resets_verification(self):
        self.visitor.phone, self.visitor.phone_verified = "11223344", True
        self.visitor.save()
        d = self.client.patch("/api/auth/me", {"phone_country": "SE", "phone": "70 123 45 67"}, format="json").data
        self.assertFalse(d["phone_verified"])
        self.assertEqual(self.client.patch("/api/auth/me", {"phone": "12"}, format="json").status_code, 400)

    def test_confirm_email_flow_with_neutral_sender(self):
        from django.core import mail
        self.client.patch("/api/auth/me", {"sender_name": "neutral"}, format="json")
        self.client.post("/api/account/email-confirmation")
        self.assertEqual(len(mail.outbox), 1)
        self.assertTrue(mail.outbox[0].from_email.startswith("Notification"))
        token = mail.outbox[0].body.split("token=")[1].split()[0]
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/account/email-confirmation/confirm", {"token": "bad"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/account/email-confirmation/confirm", {"token": token}, format="json").status_code, 200)
        self.visitor.refresh_from_db()
        self.assertTrue(self.visitor.email_confirmed)

    def test_change_email_needs_password_and_unconfirms(self):
        from django.core import mail
        self.visitor.email_confirmed = True
        self.visitor.save()
        self.assertEqual(self.client.post("/api/account/email-change", {"email": "n@test.dk", "password": "x"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/account/email-change", {"email": "adv@test.dk", "password": "pw-Strong-123"}, format="json").status_code, 400)
        d = self.client.post("/api/account/email-change", {"email": "New@Test.dk", "password": "pw-Strong-123"}, format="json").data
        self.assertEqual((d["email"], d["email_confirmed"]), ("new@test.dk", False))
        self.assertEqual(mail.outbox[-1].to, ["new@test.dk"])

    def test_change_password(self):
        url = "/api/account/password"
        self.assertEqual(self.client.post(url, {"current_password": "wrong", "new_password": "N3w-Secret-pw!", "repeat_password": "N3w-Secret-pw!"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"current_password": "pw-Strong-123", "new_password": "N3w-Secret-pw!", "repeat_password": "other"}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"current_password": "pw-Strong-123", "new_password": "N3w-Secret-pw!", "repeat_password": "N3w-Secret-pw!"}, format="json").status_code, 200)
        self.visitor.refresh_from_db()
        self.assertTrue(self.visitor.check_password("N3w-Secret-pw!"))

    def test_profile_picture_upload_and_remove(self):
        d = self.client.post("/api/account/profile-picture", {"file": make_image()}, format="multipart").data
        self.assertTrue(d["profile_picture"].endswith(".webp"))
        self.assertIsNone(self.client.delete("/api/account/profile-picture").data["profile_picture"])

    def test_delete_account(self):
        self.assertEqual(self.client.post("/api/account/delete", {"password": "nope"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/account/delete", {"password": "pw-Strong-123"}, format="json").status_code, 204)
        self.assertFalse(User.objects.filter(pk=self.visitor.pk).exists())


class ConcurrentSettingsTests(Base):
    def test_partial_saves_do_not_overwrite_each_other(self):
        from .serializers import UserSerializer
        stale = User.objects.get(pk=self.visitor.pk)  # loaded before the other save
        fresh = User.objects.get(pk=self.visitor.pk)
        ser = UserSerializer(fresh, data={"sender_name": "neutral"}, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        ser = UserSerializer(stale, data={"phone": "20304050"}, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        self.visitor.refresh_from_db()
        self.assertEqual((self.visitor.sender_name, self.visitor.phone), ("neutral", "20304050"))


class MessagingTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make_ad(title="Calm massage")
        self.ad.approve()
        self.adv.display_name = "Aurora"
        self.adv.save()

    def start(self, body="Hi, are you free on Friday?"):
        self.client.force_authenticate(self.visitor)
        res = self.client.post(f"/api/ads/{self.ad.id}/message", {"body": body}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        return res.data["conversation_id"]

    def test_full_conversation_with_unread_counts(self):
        from django.core import mail
        cid = self.start()
        self.assertEqual(self.start("Second message"), cid)  # same thread per ad + visitor
        # Advertiser got exactly one e-mail for the unread streak, without the message text.
        self.assertEqual(len(mail.outbox), 1)
        self.assertNotIn("Friday", mail.outbox[0].body)
        self.assertEqual(mail.outbox[0].to, [self.adv.email])

        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.get("/api/conversations/unread").data["unread"], 2)
        listing = self.client.get("/api/conversations").data
        self.assertEqual(listing[0]["unread"], 2)
        self.assertNotIn("@", listing[0]["other_name"])  # never expose e-mail addresses
        thread = self.client.get(f"/api/conversations/{cid}").data
        self.assertEqual([m["mine"] for m in thread["messages"]], [False, False])
        self.assertEqual(self.client.get("/api/conversations/unread").data["unread"], 0)

        self.client.post(f"/api/conversations/{cid}", {"body": "Yes, 18:00 works."}, format="json")
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.get("/api/conversations/unread").data["unread"], 1)
        self.assertEqual(self.client.get("/api/conversations").data[0]["other_name"], "Aurora")

    def test_strangers_cannot_read(self):
        cid = self.start()
        other = User.objects.create_user("x@test.dk", "pw-Strong-123")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(f"/api/conversations/{cid}").status_code, 404)
        self.assertEqual(self.client.post(f"/api/conversations/{cid}", {"body": "hi"}, format="json").status_code, 404)

    def test_cannot_message_own_ad_or_empty(self):
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.post(f"/api/ads/{self.ad.id}/message", {"body": "hi"}, format="json").status_code, 400)
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(f"/api/ads/{self.ad.id}/message", {"body": "  "}, format="json").status_code, 400)

    def test_block_and_hide(self):
        cid = self.start()
        self.client.force_authenticate(self.adv)
        self.assertTrue(self.client.post(f"/api/conversations/{cid}/block").data["blocked"])
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(f"/api/conversations/{cid}", {"body": "hello?"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"/api/conversations/{cid}/block").status_code, 403)  # only blocker can unblock
        # Hiding removes it from my list only.
        self.assertEqual(self.client.delete(f"/api/conversations/{cid}").status_code, 204)
        self.assertEqual(self.client.get("/api/conversations").data, [])
        self.client.force_authenticate(self.adv)
        self.assertEqual(len(self.client.get("/api/conversations").data), 1)


@override_settings(PAYMENTWINGS_PUBLIC_KEY="", PAYMENTWINGS_PRIVATE_KEY="", PAYMENTS_ALLOW_FAKE=True)
class EconomyTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make_ad()
        self.ad.approve()
        self.client.force_authenticate(self.adv)

    def pay(self, url, data):
        oid = self.client.post(url, data, format="json").data["order_id"]
        return self.client.post(f"/api/payments/fake/{oid}", {"result": "success"}, format="json").data

    def test_offer_discount_applies_only_when_chosen(self):
        from datetime import timedelta

        from .models import Offer
        deal = Offer.objects.create(title="Day deal", discount_percent=25, applies_to="plans", ends_at=timezone.now() + timedelta(days=1))
        # Not chosen: normal prices, no offer.
        gold = next(p for p in self.client.get("/api/plans").data if p["tier"] == "premium")["prices"][2]
        self.assertEqual((gold["list_price"], gold["price"], gold["offer"]), (39900, 39900, None))
        # Chosen ('Use offer' / code): discounted in the list and at checkout.
        gold = next(p for p in self.client.get(f"/api/plans?offer={deal.id}").data if p["tier"] == "premium")["prices"][2]
        self.assertEqual((gold["list_price"], gold["price"]), (39900, 29900))
        order = self.pay(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d", "offer": str(deal.id)})
        self.assertEqual(order["amount"], 29900)
        self.assertEqual(len(self.client.get("/api/offers").data), 1)

    def test_buy_coins_then_bump(self):
        self.pay("/api/coins/buy", {"product": "coins-260"})
        self.assertEqual(self.client.get("/api/coins").data["balance"], 260)
        self.assertEqual(self.client.post(f"/api/my-ads/{self.ad.id}/bump").data["diamonds"], 240)
        self.assertEqual([t["amount"] for t in self.client.get("/api/coins").data["log"]], [-20, 260])
        self.assertEqual(self.client.get("/api/diamonds").data["balance"], 240)  # old URL still works

    def test_card_frame_follows_the_plan(self):
        from datetime import timedelta
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["frame"], "free")
        self.ad.refresh_from_db()
        self.ad.tier, self.ad.tier_until = "premium", timezone.now() + timedelta(days=3)
        self.ad.save()
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["frame"], "premium")
        self.ad.tier_until = timezone.now() - timedelta(minutes=1)  # plan ran out
        self.ad.save()
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["frame"], "free")

    def test_card_purchase_gives_5_percent_coins_once_and_refund_takes_them_back(self):
        from . import payments
        from .models import Order
        order = self.pay(f"/api/my-ads/{self.ad.id}/checkout", {"product": "premium-30d"})  # 399 kr
        self.assertEqual(self.client.get("/api/coins").data["balance"], 19)  # 5 % of 399, whole coins
        o = Order.objects.get(order_id=order["order_id"])
        payments.apply_gateway_state(o.pk, o.last_gateway_state)  # gateway reports "paid" again
        self.assertEqual(self.client.get("/api/coins").data["balance"], 19)
        payments.refund_order(Order.objects.get(pk=o.pk))
        self.assertEqual(self.client.get("/api/coins").data["balance"], 0)

    def test_spend_coins_on_tier_and_bump(self):
        from datetime import timedelta

        from .economy import add_diamonds
        self.ad.refresh_from_db()
        self.ad.expires_at = timezone.now() + timedelta(days=10)
        self.ad.save()
        add_diamonds(self.adv, 200, "test")
        ad_row = self.client.get("/api/coins").data["ads"][0]
        codes = {a["code"]: a for a in ad_row["addons"]}
        self.assertFalse(codes["premium-30d"]["fits"])  # would run past the ad's end
        self.assertTrue(codes["premium-7d"]["fits"])
        spend = lambda items: self.client.post("/api/coins/spend", {"ad": str(self.ad.id), "items": items}, format="json")
        self.assertEqual(spend(["premium-30d"]).status_code, 400)
        self.assertEqual(spend(["premium-7d", "vip-7d"]).status_code, 400)  # only one tier option
        r = spend(["premium-7d", "bump"])  # 149 + 20
        self.assertEqual(r.data, {"spent": 169, "balance": 31})
        self.ad.refresh_from_db()
        self.assertEqual(self.ad.effective_tier, "premium")
        self.assertEqual(spend(["premium-7d"]).status_code, 400)  # not enough coins

    def test_ad_details_and_similar(self):
        res = self.client.patch(f"/api/my-ads/{self.ad.id}", {
            "height_cm": 168, "body_type": "slim", "services": [{"name": "60 min", "price": "800 kr"}, {"name": ""}],
            "contact_hours": {"mon": "10-18", "xyz": "no"}}, format="json")
        self.assertEqual((res.data["services"], res.data["contact_hours"]), ([{"name": "60 min", "price": "800 kr"}], {"mon": "10-18"}))
        self.ad.refresh_from_db()
        self.ad.approve()
        other = self.make_ad(title="Other")
        other.approve()
        d = self.client.get(f"/api/ads/{self.ad.id}").data
        self.assertEqual(d["body_type_label"], "Slim")
        self.assertEqual([a["title"] for a in self.client.get(f"/api/ads/{self.ad.id}/similar").data["ads"]], ["Other"])


from rest_framework.test import APITransactionTestCase


class RealtimeTests(APITransactionTestCase):
    # Channels closes database connections between async calls (like a real server). That breaks the
    # transaction a normal TestCase wraps around each test on PostgreSQL, so this test commits for real.
    # (Django runs TransactionTestCase classes after all TestCase classes, so its cleanup doesn't affect them.)
    setUp = Base.setUp
    make_ad = Base.make_ad

    async def test_socket_receives_new_message(self):
        from asgiref.sync import sync_to_async
        from channels.testing import WebsocketCommunicator

        from .messaging import post_message
        from .models import Conversation
        from .realtime import MessagesConsumer

        ad = await sync_to_async(self.make_ad)()
        conv = await sync_to_async(Conversation.objects.create)(
            ad=ad, ad_title=ad.title, visitor=self.visitor, advertiser=self.adv)

        anon = WebsocketCommunicator(MessagesConsumer.as_asgi(), "/ws/messages/")
        anon.scope["user"] = None
        connected, _ = await anon.connect()
        self.assertFalse(connected)

        ws = WebsocketCommunicator(MessagesConsumer.as_asgi(), "/ws/messages/")
        ws.scope["user"] = self.adv
        connected, _ = await ws.connect()
        self.assertTrue(connected)

        await sync_to_async(post_message)(conv, self.visitor, "Hello live")  # autocommit: pushes right away

        event = await ws.receive_json_from(timeout=2)
        self.assertEqual((event["conversation_id"], event["message"]["body"], event["message"]["mine"]),
                         (conv.pk, "Hello live", False))
        await ws.disconnect()


class FavoritesCommentsProfileTests(Base):
    def setUp(self):
        super().setUp()
        self.ad = self.make_ad(title="Live ad")
        self.ad.approve()
        old = self.make_ad(title="Old ad")
        old.approve()
        old.expires_at = timezone.now() - timedelta(days=1)
        old.save()

    def test_favorites(self):
        self.client.force_authenticate(self.visitor)
        self.assertTrue(self.client.post(f"/api/ads/{self.ad.id}/favorite").data["favorite"])
        self.assertEqual([a["title"] for a in self.client.get("/api/favorites").data["results"]], ["Live ad"])
        self.assertFalse(self.client.delete(f"/api/ads/{self.ad.id}/favorite").data["favorite"])

    def test_comments(self):
        url = f"/api/ads/{self.ad.id}/comments"
        self.assertEqual(self.client.post(url, {"body": "hi"}, format="json").status_code, 403)
        self.client.force_authenticate(self.visitor)
        cid = self.client.post(url, {"body": "Lovely place!"}, format="json").data["id"]
        self.assertEqual(self.client.post(url, {"body": "  "}, format="json").status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.get(url).data[0]["body"], "Lovely place!")
        stranger = User.objects.create_user("s@test.dk", "pw-Strong-123")
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.delete(f"/api/comments/{cid}").status_code, 403)
        self.client.force_authenticate(self.adv)  # ad owner may remove comments
        self.assertEqual(self.client.delete(f"/api/comments/{cid}").status_code, 204)

    def test_profile_with_history_and_opt_out(self):
        d = self.client.get(f"/api/advertisers/{self.adv.pk}").data
        self.assertEqual(([a["title"] for a in d["active_ads"]], [a["title"] for a in d["past_ads"]]), (["Live ad"], ["Old ad"]))
        self.assertNotIn("email", d)
        self.adv.show_ad_history = False
        self.adv.save()
        self.assertEqual(self.client.get(f"/api/advertisers/{self.adv.pk}").data["past_ads"], [])
        self.assertEqual(self.client.get(f"/api/advertisers/{self.visitor.pk}").status_code, 404)


import tempfile

_private_tmp = tempfile.mkdtemp()


@override_settings(PRIVATE_MEDIA_ROOT=_private_tmp)
class IdVerificationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user("v@test.dk", "pw-Strong-123", is_advertiser=True, display_name="Vera")
        self.client.force_authenticate(self.user)

    def payload(self, **kw):
        data = {"document_type": "passport", "full_name": "Vera Test", "date_of_birth": "1995-04-02",
                "consent": "true", "document_front": make_image(), "selfie": make_image("s.jpg")}
        data.update(kw)
        return data

    def test_under_18_and_missing_files_are_rejected(self):
        r = self.client.post("/api/account/verification", self.payload(date_of_birth=str(timezone.localdate().replace(year=timezone.localdate().year - 17)), selfie=""), format="multipart")
        self.assertEqual(r.status_code, 400)
        self.assertIn("date_of_birth", r.data)
        self.assertIn("selfie", r.data)
        r = self.client.post("/api/account/verification", self.payload(document_type="id_card"), format="multipart")
        self.assertIn("document_back", r.data)  # ID cards need the back side

    def test_visitor_converts_only_by_sending_documents(self):
        visitor = User.objects.create_user("vis@test.dk", "pw-Strong-123")
        self.client.force_authenticate(visitor)
        self.assertEqual(self.client.patch("/api/auth/me", {"is_advertiser": True}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/account/verification", self.payload(), format="multipart").status_code, 400)
        r = self.client.post("/api/account/verification", self.payload(selfie=""), format="multipart")  # incomplete
        visitor.refresh_from_db()
        self.assertFalse(visitor.is_advertiser)
        r = self.client.post("/api/account/verification", self.payload(become_advertiser="true"), format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        visitor.refresh_from_db()
        self.assertTrue(visitor.is_advertiser)
        self.assertFalse(visitor.age_verified)  # still waits for the moderator

    def test_submit_review_and_badge(self):
        from .models import IdVerification

        r = self.client.post("/api/account/verification", self.payload(), format="multipart")
        self.assertEqual(r.status_code, 201, r.data)
        self.assertEqual(r.data["latest"]["status"], "pending")
        v = IdVerification.objects.get(user=self.user)
        self.assertTrue(v.document_front.path.startswith(_private_tmp))  # never under the public MEDIA_ROOT
        # A second submission while pending is refused.
        self.assertEqual(self.client.post("/api/account/verification", self.payload(), format="multipart").status_code, 400)

        # Only staff can open the document pictures.
        self.client.force_authenticate(None)
        url = f"/admin/core/idverification/{v.pk}/file/document_front/"
        self.assertEqual(self.client.get(url).status_code, 302)
        staff = User.objects.create_superuser("boss@test.dk", "pw-Strong-123")
        self.client.force_login(staff)
        self.assertEqual(self.client.get(url).status_code, 200)

        path = v.document_front.path
        v.decide(True, by=staff)
        self.user.refresh_from_db()
        self.assertTrue(self.user.age_verified)
        self.assertFalse(v.files)
        import os
        self.assertFalse(os.path.exists(path))  # documents deleted after review
        self.assertTrue(self.client.get(f"/api/advertisers/{self.user.pk}").data["verified"])

    def test_age_verified_cannot_be_set_by_user(self):
        self.client.patch("/api/auth/me", {"age_verified": True}, format="json")
        self.user.refresh_from_db()
        self.assertFalse(self.user.age_verified)


class OfferPhotoTests(APITestCase):
    def test_offer_photo_is_cleaned_and_listed(self):
        from datetime import timedelta

        from .models import Offer

        buf = BytesIO()
        img = Image.new("RGB", (80, 40), "red")
        exif = img.getexif()
        exif[0x010F] = "SecretCam"  # camera make – must be stripped
        img.save(buf, "JPEG", exif=exif)
        o = Offer.objects.create(title="Spring", discount_percent=15, ends_at=timezone.now() + timedelta(days=2),
                                 image=SimpleUploadedFile("b.jpg", buf.getvalue(), content_type="image/jpeg"))
        self.assertTrue(o.image.name.startswith("offers/") and o.image.name.endswith(".webp"))
        self.assertNotIn(b"SecretCam", o.image.read())
        Offer.objects.create(title="Old", discount_percent=10, ends_at=timezone.now() - timedelta(days=1))
        data = self.client.get("/api/offers").data
        self.assertEqual([x["title"] for x in data], ["Spring"])  # expired offers are hidden
        self.assertTrue(data[0]["image"].endswith(".webp"))
        self.assertTrue(data[0]["show_on_home"])
        o.image.delete(save=False)


class LookupTableTests(Base):
    def test_lists_come_from_the_database(self):
        from .models import AdCategory, AdRegion, ChoiceOption, Country
        se = Country.objects.get(code="SE")
        se.has_ads = True
        se.save()
        AdRegion.objects.create(country=se, slug="stockholm", name="Stockholm")
        AdCategory.objects.filter(slug="event").update(is_active=False)
        AdCategory.objects.get(slug="massage").save()  # any save clears the cache
        ChoiceOption.objects.filter(group="gender", value="female").update(label="Female")
        ChoiceOption.objects.get(group="gender", value="male").save()
        meta = self.client.get("/api/meta").data
        self.assertIn({"value": "stockholm", "label": "Stockholm", "country": "SE"}, meta["regions"])
        self.assertNotIn("event", [c["value"] for c in meta["categories"]])
        self.assertEqual(next(g for g in meta["genders"] if g["value"] == "female")["label"], "Female")
        self.assertEqual({c["code"] for c in meta["countries"]} >= {"DK", "SE"}, True)
        self.assertEqual(len(meta["eye_colors"]), 5)

        self.client.force_authenticate(self.adv)
        base = {"title": "My ad", "description": "Hello", "category": "massage", "gender": "female", "age": 25}
        self.assertEqual(self.client.post("/api/my-ads", {**base, "region": "atlantis"}, format="json").status_code, 400)
        self.assertEqual(self.client.post("/api/my-ads", {**base, "category": "event", "region": "dharan"}, format="json").status_code, 400)
        ad_id = self.client.post("/api/my-ads", {**base, "region": "stockholm"}, format="json").data["id"]
        from .models import Ad
        Ad.objects.get(pk=ad_id).approve()
        card = self.client.get(f"/api/ads/{ad_id}").data
        self.assertEqual((card["region_label"], card["country"], card["gender_label"]), ("Stockholm", "SE", "Female"))
        self.assertEqual([a["id"] for a in self.client.get("/api/ads?country=SE").data["results"]], [ad_id])
        # Phone countries follow the Country table.
        self.assertEqual(self.client.patch("/api/auth/me", {"phone_country": "SE"}, format="json").status_code, 200)
        self.assertEqual(self.client.patch("/api/auth/me", {"phone_country": "ZZ"}, format="json").status_code, 400)


class RichDescriptionTests(Base):
    def test_formatting_kept_and_dangerous_html_removed(self):
        self.client.force_authenticate(self.adv)
        html = ('<h2>About me</h2><p><strong>Hi</strong> <em>there</em> <u>you</u>'
                '<img src=x onerror=alert(1)><script>alert(2)</script><a href="javascript:x">link</a></p>'
                '<ul><li><p>One</p></li></ul><p style="color:red" onclick="x()">Two</p>')
        res = self.client.post("/api/my-ads", {"title": "My ad", "description": html, "category": "massage",
                                                "gender": "female", "region": "dharan", "age": 25}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        d = res.data["description"]
        for kept in ("<h2>About me</h2>", "<strong>Hi</strong>", "<em>there</em>", "<u>you</u>", "<ul><li><p>One</p></li></ul>", "<p>Two</p>"):
            self.assertIn(kept, d)
        for gone in ("<img", "onerror", "<script", "alert(2)", "<a", "javascript", "style=", "onclick"):
            self.assertNotIn(gone, d)
        from .models import Ad
        ad = Ad.objects.get(pk=res.data["id"])
        ad.approve()
        card = self.client.get(f"/api/ads/{ad.id}").data
        self.assertTrue(card["excerpt"].startswith("About me Hi there you"))
        # Empty formatting is not a description.
        r = self.client.patch(f"/api/my-ads/{ad.id}", {"description": "<p> </p><p><br></p>"}, format="json")
        self.assertEqual(r.status_code, 400)
        # Plain text from older ads still works.
        r = self.client.patch(f"/api/my-ads/{ad.id}", {"description": "Plain\n\ntext"}, format="json")
        self.assertEqual(r.data["description"], "Plain\n\ntext")



class CreateAdExtrasTests(Base):
    def test_places_belong_to_the_chosen_areas(self):
        from .models import AdPlace, AdRegion
        pokhara = AdRegion.objects.get(slug="pokhara")
        AdPlace.objects.get_or_create(region=pokhara, name="Satmuhane", defaults={"slug": "satmuhane"})
        self.client.force_authenticate(self.adv)
        res = self.client.post("/api/my-ads", {
            "title": "Places", "description": "Hi", "categories": ["massage"], "gender": "female", "age": 30,
            "regions": ["pokhara", "dharan"], "places": ["satmuhane", "lakeside"], "audiences": ["men"],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["places"], res.data["city"]), (["satmuhane", "lakeside"], "Satmuhane"))
        ad_id = res.data["id"]
        from rest_framework.test import APIRequestFactory

        from .models import Ad
        from .serializers import AdDetailSerializer
        req = APIRequestFactory().get("/")
        req.user = self.adv
        loc = AdDetailSerializer(Ad.objects.get(pk=ad_id), context={"request": req}).data["location"]
        self.assertEqual([(c["code"], [(r["value"], [p["value"] for p in r["places"]]) for r in c["regions"]]) for c in loc["countries"]],
                         [("NP", [("pokhara", ["satmuhane", "lakeside"]), ("dharan", [])])])
        # Places can also be typed in (kept even when areas change) …
        d = self.client.patch(f"/api/my-ads/{ad_id}", {"places": ["Hemja", "satmuhane"]}, format="json").data
        self.assertEqual((d["places"], d["city"]), (["Hemja", "satmuhane"], "Hemja"))
        self.assertEqual(self.client.patch(f"/api/my-ads/{ad_id}", {"places": ["123"]}, format="json").status_code, 400)
        # … and dropping an area drops its listed places.
        d = self.client.patch(f"/api/my-ads/{ad_id}", {"regions": ["dharan"]}, format="json").data
        self.assertEqual(d["places"], ["Hemja"])

    def test_several_areas_categories_price_ranges_tags_and_replace(self):
        review_on()  # this test covers the moderator flow
        self.client.force_authenticate(self.adv)
        res = self.client.post("/api/my-ads", {
            "title": "Multi", "description": "Hello", "categories": ["massage", "escort"], "gender": "female",
            "regions": ["dharan", "pokhara"], "age": 30, "phone_country": "SE", "phone": "70 123 45 67", "audiences": ["men"],
            "services": [{"kind": "heading", "name": "Massage", "price": "999"}, {"kind": "range", "name": "60 min", "price": "800", "price_to": "1000"}],
        }, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        d = res.data
        self.assertEqual((d["category"], d["categories"], d["region"], d["regions"]), ("massage", ["massage", "escort"], "dharan", ["dharan", "pokhara"]))
        self.assertEqual(d["services"], [{"name": "Massage", "price": "", "kind": "heading"}, {"name": "60 min", "price": "800", "price_to": "1000", "kind": "range"}])
        ad_id = d["id"]
        self.assertEqual(self.client.patch(f"/api/my-ads/{ad_id}", {"regions": ["atlantis"]}, format="json").status_code, 400)
        # Submitting without pictures is allowed (the editor warns first).
        self.assertEqual(self.client.post(f"/api/my-ads/{ad_id}/submit").data["status"], "pending")
        from .models import Ad
        Ad.objects.get(pk=ad_id).approve()
        # Shown in the second area and category too.
        self.assertIn(ad_id, [a["id"] for a in self.client.get("/api/ads?region=pokhara").data["results"]])
        self.assertIn(ad_id, [a["id"] for a in self.client.get("/api/ads?category=escort").data["results"]])
        self.assertNotIn(ad_id, [a["id"] for a in self.client.get("/api/ads?region=bornholm").data["results"]])
        detail = self.client.get(f"/api/ads/{ad_id}").data
        self.assertEqual((detail["region_labels"], detail["category_labels"], detail["phone_dial"]), (["Dharan", "Pokhara"], ["Massage", "Escort"], "+46"))

        # Own tags: created once, then reused (case-insensitive).
        r1 = self.client.post("/api/tags", {"name": "Hot  stone"}, format="json")
        self.assertEqual((r1.status_code, r1.data["name"]), (201, "Hot stone"))
        self.assertEqual(self.client.post("/api/tags", {"name": "hot stone"}, format="json").status_code, 200)
        self.assertEqual(self.client.post("/api/tags", {"name": "<b>"}, format="json").status_code, 400)
        self.client.force_authenticate(None)
        self.assertEqual(self.client.post("/api/tags", {"name": "Anon"}, format="json").status_code, 403)

        # Replace a picture (crop) keeps its place.
        self.client.force_authenticate(self.adv)
        pics = self.client.post(f"/api/my-ads/{ad_id}/pictures", {"files": [make_image(), make_image("b.jpg")]}, format="multipart").data["pictures"]
        res = self.client.post(f"/api/my-ads/{ad_id}/pictures/{pics[1]['id']}/replace", {"file": make_image("c.jpg")}, format="multipart")
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual([p["id"] for p in res.data["pictures"]], [pics[0]["id"], pics[1]["id"]])
        self.assertNotEqual(res.data["pictures"][1]["url"], pics[1]["url"])


    def test_draft_can_start_with_title_only_and_activate_checks_the_rest(self):
        self.client.force_authenticate(self.adv)
        res = self.client.post("/api/my-ads", {"title": "Just started", "description": "<p>Hi</p>"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual((res.data["gender"], res.data["age"], res.data["categories"]), ("", None, []))
        r = self.client.post(f"/api/my-ads/{res.data['id']}/submit")
        self.assertEqual(r.status_code, 400)
        self.assertIn("categories, areas, offers to, gender, age", r.data["detail"])
        lines = [{"kind": "from", "name": "Massage", "price": "600", "price_to": "999"}, {"name": "Old line", "price": "5", "price_to": "9"}]
        r = self.client.patch(f"/api/my-ads/{res.data['id']}", {"services": lines}, format="json")
        self.assertEqual(r.data["services"], [{"name": "Massage", "price": "600", "kind": "from"}, {"name": "Old line", "price": "5", "price_to": "9", "kind": "range"}])


class MyAdsPageTests(Base):
    def test_views_duplicate_delete_and_completeness(self):
        from django.core.cache import cache
        cache.clear()
        ad = self.make_ad()
        ad.approve()
        # Visitors: counted once per 2 minutes; the owner's own views don't count.
        self.client.get(f"/api/ads/{ad.id}")
        self.client.get(f"/api/ads/{ad.id}")
        self.client.force_authenticate(self.adv)
        self.client.get(f"/api/ads/{ad.id}")
        mine = self.client.get("/api/my-ads").data[0]
        self.assertEqual(mine["views_30d"], 1)
        self.assertTrue(mine["is_live"])
        self.assertEqual(mine["missing_fields"], [])
        self.assertTrue(0 < mine["complete_percent"] <= 100)
        v = self.client.get(f"/api/my-ads/{ad.id}/visits").data
        self.assertEqual((len(v["days"]), v["total"], v["days"][-1]["count"]), (30, 1, 1))
        self.assertEqual(v["lanes"][0]["kind"], "ad")

        # A live ad can't be deleted; a duplicate is a draft copy that can.
        self.assertEqual(self.client.delete(f"/api/my-ads/{ad.id}").status_code, 403)
        self.client.post(f"/api/my-ads/{ad.id}/pictures", {"files": [make_image()]}, format="multipart")
        dup = self.client.post(f"/api/my-ads/{ad.id}/duplicate")
        self.assertEqual(dup.status_code, 201, dup.data)
        self.assertEqual((dup.data["status"], dup.data["title"], len(dup.data["pictures"]), dup.data["views_30d"]),
                         ("draft", ad.title + " (copy)", 1, 0))
        self.assertNotEqual(dup.data["pictures"][0]["url"], self.client.get(f"/api/my-ads/{ad.id}").data["pictures"][0]["url"])
        self.assertEqual(self.client.delete(f"/api/my-ads/{dup.data['id']}").status_code, 204)

        # Missing fields are listed for an unfinished draft.
        draft = self.client.post("/api/my-ads", {"title": "New", "description": "x"}, format="json").data
        self.assertEqual(draft["missing_fields"], ["categories", "areas", "offers to", "gender", "age"])



class ActivationTests(Base):
    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.adv)

    def ready_ad(self, title="Test ad"):
        return self.client.post("/api/my-ads", {"title": title, "description": "Hello there", "categories": ["massage"],
                                                "regions": ["dharan"], "audiences": ["men"], "gender": "female", "age": 30}, format="json").data["id"]

    def activate(self, ad_id, product, method="card"):
        return self.client.post(f"/api/my-ads/{ad_id}/activate", {"product": product, "method": method}, format="json")

    def test_free_is_one_week_and_only_one_at_a_time(self):
        review_on()  # this test covers the moderator flow
        from .models import Ad
        a, b = self.ready_ad("First ad"), self.ready_ad("Second ad")
        self.assertTrue(self.client.get(f"/api/my-ads/{a}/activate").data["free_allowed"])
        r = self.activate(a, "free-7d")
        self.assertEqual(r.data.get("status"), "done", r.data)
        ad = Ad.objects.get(pk=a)
        self.assertEqual((ad.status, ad.pending_days, ad.free_listing), ("pending", 7, True))
        # A second Free ad is refused while the first is in review or live.
        opts = self.client.get(f"/api/my-ads/{b}/activate").data
        self.assertEqual((opts["free_allowed"], opts["free_blocked_by"]["title"]), (False, "First ad"))
        r = self.activate(b, "free-7d")
        self.assertEqual(r.status_code, 400)
        self.assertIn("Only one Free ad", r.data["detail"])
        self.assertEqual(self.client.post(f"/api/my-ads/{b}/submit").status_code, 400)  # submit = Free too
        ad.approve()
        ad.refresh_from_db()
        self.assertAlmostEqual((ad.expires_at - timezone.now()).total_seconds() / 86400, 7, delta=0.01)
        # When the free week is over, a new Free ad is allowed again.
        Ad.objects.filter(pk=a).update(expires_at=timezone.now() - timedelta(minutes=1))
        self.assertTrue(self.client.get(f"/api/my-ads/{b}/activate").data["free_allowed"])

    def test_paid_days_start_at_approval_card_and_coins(self):
        review_on()  # this test covers the moderator flow
        from .economy import add_diamonds
        from .models import Ad
        a = self.ready_ad()
        r = self.activate(a, "premium-7d", "card")
        self.assertEqual(r.data["status"], "redirect")
        self.client.post(f"/api/payments/fake/{r.data['order_id']}", {"result": "success"}, format="json")
        ad = Ad.objects.get(pk=a)
        self.assertEqual((ad.status, ad.pending_tier, ad.pending_days, ad.tier_until), ("pending", "premium", 7, None))
        ad.approve()
        ad.refresh_from_db()
        self.assertEqual(ad.effective_tier, "premium")
        self.assertAlmostEqual((ad.expires_at - timezone.now()).total_seconds() / 86400, 7, delta=0.01)

        b = self.ready_ad("Coins")
        self.assertEqual(self.activate(b, "standard-7d", "coins").status_code, 400)  # no coins yet
        add_diamonds(self.adv, 100, "test")
        self.adv.refresh_from_db()
        before = self.adv.diamonds  # 100 + 5 % cashback from the Gold card payment
        from .economy import price_for
        from .models import Product
        coins = price_for(Product.objects.get(code="standard-7d"))[0] // 100  # 1 coin = 1 kr
        r = self.activate(b, "standard-7d", "coins")
        self.assertEqual(r.data["status"], "done", r.data)
        self.adv.refresh_from_db()
        self.assertEqual(self.adv.diamonds, before - coins)
        self.assertEqual(Ad.objects.get(pk=b).pending_tier, "standard")


    def test_addons_are_charged_and_switched_on_when_live(self):
        review_on()  # this test covers the moderator flow
        from .economy import add_diamonds
        from .models import Ad, AddOn, Order, Product
        opts = self.client.get(f"/api/my-ads/{self.ready_ad()}/activate").data
        groups = {g["effect"]: g for g in opts["addons"]}
        self.assertEqual([o["label"] for o in groups["top"]["options"]], ["7 days", "14 days", "30 days", "60 days"])
        self.assertEqual(groups["top"]["badge"], "Featured")
        AddOn.objects.filter(code="top-7d").update(price=5000)  # prices come from the admin
        a = self.ready_ad("With add-ons")
        r = self.client.post(f"/api/my-ads/{a}/activate", {"product": "premium-7d", "method": "card",
                                                            "addons": ["top-7d", "slider-7d"]}, format="json")
        self.assertEqual(r.data["status"], "redirect", r.data)
        order = Order.objects.get(order_id=r.data["order_id"])
        plan = Product.objects.get(code="premium-7d").price
        self.assertEqual(order.amount, plan + 5000 + AddOn.objects.get(code="slider-7d").price)
        self.assertEqual(sorted(x["code"] for x in order.addons), ["slider-7d", "top-7d"])
        self.client.post(f"/api/payments/fake/{order.order_id}", {"result": "success"}, format="json")
        ad = Ad.objects.get(pk=a)
        self.assertIsNone(ad.top_until)  # waits for approval, so no days are lost in review
        ad.approve()
        ad.refresh_from_db()
        self.assertAlmostEqual((ad.top_until - timezone.now()).total_seconds() / 86400, 7, delta=0.01)
        self.assertTrue(Order.objects.get(pk=order.pk).addons_applied)
        card = self.client.get(f"/api/ads/{a}").data
        self.assertNotIn("highlighted", card)
        self.assertTrue(card["featured"])
        self.assertFalse(self.client.get(f"/api/ads/{self.ready_ad()}").data.get("featured", False))
        self.assertIn(a, [x["id"] for x in self.client.get("/api/ads/front").data["featured"]])

        # A live ad: add-ons with coins start at once; unknown codes are refused.
        bad = self.client.post(f"/api/my-ads/{a}/activate", {"product": "premium-7d", "method": "coins", "addons": ["nope"]}, format="json")
        self.assertEqual(bad.status_code, 400)
        add_diamonds(self.adv, 10_000, "test")
        before = ad.bumped_at
        two = self.client.post(f"/api/my-ads/{a}/activate", {"product": "premium-7d", "method": "coins", "addons": ["top-7d", "top-14d"]}, format="json")
        self.assertEqual(two.status_code, 400)  # one period per add-on
        r = self.client.post(f"/api/my-ads/{a}/activate", {"product": "standard-7d", "method": "coins", "addons": ["bump-0d", "top-7d"]}, format="json")
        self.assertEqual(r.data["status"], "done", r.data)
        ad.refresh_from_db()
        self.assertGreater(ad.bumped_at, before)
        self.assertGreater(ad.top_until, timezone.now() + timedelta(days=6))


    def test_slider_has_limited_slots(self):
        from .economy import add_diamonds
        from .models import Ad, SiteSettings
        site = SiteSettings.load()
        site.slider_slots = 1
        site.save()
        add_diamonds(self.adv, 100_000, "test")
        first, second = self.ready_ad("First"), self.ready_ad("Second")
        for ad_id in (first, second):
            Ad.objects.get(pk=ad_id).approve()
        buy = lambda ad_id: self.client.post(f"/api/my-ads/{ad_id}/activate", {"product": "premium-7d", "method": "coins", "addons": ["slider-7d"]}, format="json")
        self.assertEqual(self.client.get(f"/api/my-ads/{first}/activate").data["slider_slots"]["free"], 1)
        self.assertEqual(buy(first).status_code, 201)
        front = self.client.get("/api/ads/front").data
        self.assertEqual(([a["id"] for a in front["featured"]], front["slider_slots"]["free"]), ([first], 0))
        r = buy(second)
        self.assertEqual(r.status_code, 400)
        self.assertIn("slots are taken", str(r.data))
        self.assertEqual(buy(first).status_code, 201)  # the ad that has the slot can extend it
        # Without the add-on the slider is empty – a VIP plan alone doesn't put an ad there.
        Ad.objects.filter(pk=first).update(slider_until=None)
        self.assertEqual(self.client.get("/api/ads/front").data["featured"], [])


    def test_plans_include_their_extras(self):
        from .economy import add_diamonds
        from .models import Ad, SiteSettings
        add_diamonds(self.adv, 100_000, "test")
        a = self.ready_ad("VIP ad")
        Ad.objects.get(pk=a).approve()
        buy = lambda product, addons: self.client.post(f"/api/my-ads/{a}/activate", {"product": product, "method": "coins", "addons": addons}, format="json")
        r = buy("premium-7d", ["bump-0d"])
        self.assertEqual(r.status_code, 400)
        self.assertIn("included in Premium", str(r.data))
        self.assertEqual(buy("vip-7d", ["slider-7d"]).status_code, 400)
        self.assertEqual(buy("vip-7d", ["top-7d"]).status_code, 400)
        self.assertEqual(buy("vip-7d", []).status_code, 201)
        ad = Ad.objects.get(pk=a)
        self.assertEqual((ad.top_until, ad.slider_until), (ad.tier_until, ad.tier_until))  # top of lists + slider
        self.assertTrue(self.client.get(f"/api/ads/{a}").data["featured"])
        self.assertIn(a, [x["id"] for x in self.client.get("/api/ads/front").data["featured"]])
        # With every slider slot taken, VIP still gives top of the lists but no slider.
        site = SiteSettings.load()
        site.slider_slots = 1
        site.save()
        b = self.ready_ad("Second VIP")
        Ad.objects.get(pk=b).approve()
        self.assertEqual(self.client.post(f"/api/my-ads/{b}/activate", {"product": "vip-7d", "method": "coins"}, format="json").status_code, 201)
        ad_b = Ad.objects.get(pk=b)
        self.assertIsNone(ad_b.slider_until)
        self.assertIsNotNone(ad_b.top_until)


    def test_without_review_ads_go_live_at_once(self):
        from .models import Ad
        a = self.ready_ad("Live now")
        r = self.client.post(f"/api/my-ads/{a}/activate", {"product": "premium-7d", "method": "card", "addons": ["top-7d"]}, format="json")
        self.client.post(f"/api/payments/fake/{r.data['order_id']}", {"result": "success"}, format="json")
        ad = Ad.objects.get(pk=a)
        self.assertEqual((ad.status, ad.effective_tier, ad.pending_tier), ("active", "premium", ""))
        self.assertAlmostEqual((ad.tier_until - timezone.now()).total_seconds() / 86400, 7, delta=0.01)
        self.assertIsNotNone(ad.top_until)  # add-ons start at once too
        self.assertEqual(self.client.get(f"/api/ads/{a}").status_code, 200)
        # Free (submit) is live at once, and editing a live ad keeps it live.
        b = self.ready_ad("Free now")
        self.assertEqual(self.client.post(f"/api/my-ads/{b}/submit").data["status"], "active")
        self.client.patch(f"/api/my-ads/{b}", {"title": "Free now, edited"}, format="json")
        self.assertEqual(Ad.objects.get(pk=b).status, "active")


    def test_status_set_to_active_by_hand_starts_the_plan(self):
        from .models import Ad
        review_on()
        a = self.ready_ad("Set live in admin")
        r = self.client.post(f"/api/my-ads/{a}/activate", {"product": "vip-7d", "method": "card"}, format="json")
        self.client.post(f"/api/payments/fake/{r.data['order_id']}", {"result": "success"}, format="json")
        ad = Ad.objects.get(pk=a)
        self.assertEqual((ad.status, ad.pending_tier), ("pending", "vip"))
        ad.status = "active"  # what the admin form does – no approve() call
        ad.save()
        ad.refresh_from_db()
        self.assertEqual((ad.effective_tier, ad.pending_tier, ad.pending_days), ("vip", "", None))
        self.assertIsNotNone(ad.published_at)
        self.assertAlmostEqual((ad.expires_at - timezone.now()).total_seconds() / 86400, 7, delta=0.01)
        self.assertEqual(ad.slider_until, ad.tier_until)  # VIP extras too
        self.assertEqual(self.client.get(f"/api/ads/{a}").data["tier"], "vip")

    def test_lower_waiting_plan_never_downgrades(self):
        from datetime import timedelta as td

        from .models import Ad
        a = self.ready_ad("Premium already")
        Ad.objects.filter(pk=a).update(status="active", tier="premium", tier_until=timezone.now() + td(days=20),
                                       published_at=timezone.now(), expires_at=timezone.now() + td(days=20),
                                       pending_tier="standard", pending_days=90)
        ad = Ad.objects.get(pk=a)
        ad.save()
        ad.refresh_from_db()
        self.assertEqual((ad.tier, ad.pending_tier), ("premium", ""))
        self.assertAlmostEqual((ad.expires_at - timezone.now()).total_seconds() / 86400, 90, delta=0.01)


class MoneySettingsTests(Base):
    def test_currency_and_coin_value_from_site_settings(self):
        from .economy import coins_for_amount, coins_for_price
        from .models import Product, SiteSettings
        s = SiteSettings.load()
        self.assertEqual((s.currency, s.coin_value), ("USD", 100))
        self.assertEqual((coins_for_price(14900), coins_for_amount(39900)), (149, 19))  # $1 per coin
        s.coin_value = 50  # 1 coin = $0.50
        s.currency = "EUR"
        s.save()
        self.assertEqual((coins_for_price(14900), coins_for_amount(39900)), (298, 39))
        self.assertEqual(set(Product.objects.values_list("currency", flat=True)), {"EUR"})  # prices follow the site currency
        meta = self.client.get("/api/meta").data
        self.assertEqual((meta["currency"], meta["coin_value"]), ("EUR", 50))


class OffersPageTests(Base):
    """GET /api/offers/mine – statuses (active/used/sold out/ended), codes, and once-per-advertiser pricing."""

    def setUp(self):
        super().setUp()
        from .models import Offer
        now = timezone.now()
        self.once = Offer.objects.create(title="Once", discount_percent=20, applies_to="plans", code="ONCE20",
                                         once_per_user=True, ends_at=now + timedelta(days=3))
        self.capped = Offer.objects.create(title="Capped", discount_percent=10, max_uses=1, ends_at=now + timedelta(days=3))
        self.old = Offer.objects.create(title="Old", discount_percent=50, ends_at=now - timedelta(days=1))
        self.gone = Offer.objects.create(title="Long gone", discount_percent=50, ends_at=now - timedelta(days=10))
        self.forever = Offer.objects.create(title="Forever", discount_percent=5, applies_to="diamonds", no_end_date=True,
                                            ends_at=now - timedelta(days=30))

    def test_login_required_and_statuses(self):
        from .economy import price_for
        self.assertIn(self.client.get("/api/offers/mine").status_code, (401, 403))
        self.client.force_authenticate(self.adv)
        ad = self.make_ad()
        plan = Product.objects.get(code="standard-7d")
        self.assertEqual(price_for(plan, self.adv)[1], None)  # nothing chosen: normal price
        self.assertEqual(price_for(plan, self.adv, self.once)[1], self.once)
        # The advertiser pays once with "Once"; a different user uses up "Capped".
        Order.objects.create(user=self.adv, ad=ad, product=plan, product_name="x", tier="standard", days=7, amount=1,
                             currency="USD", status="paid", offer=self.once)
        Order.objects.create(user=self.visitor, product=plan, product_name="x", tier="standard", days=7, amount=1,
                             currency="USD", status="paid", offer=self.capped)
        d = self.client.get("/api/offers/mine").data
        st = {o["title"]: o["status"] for o in d["offers"]}
        self.assertEqual(st, {"Forever": "active", "Once": "used", "Capped": "sold_out", "Old": "ended"})
        self.assertEqual([o["code"] for o in d["offers"] if o["title"] == "Once"], ["ONCE20"])
        self.assertEqual(d["ads"][0]["id"], str(ad.id))
        self.assertEqual({o["title"] for o in self.client.get("/api/offers").data}, {"Once", "Forever"})

    def test_chosen_offer_is_applied_and_checked(self):
        from .models import Ad
        self.client.force_authenticate(self.adv)
        ad = self.make_ad(status=Ad.Status.ACTIVE, expires_at=timezone.now() + timedelta(days=20))
        # Check by id or code (any case); used / unknown codes are refused with a reason.
        self.assertEqual(self.client.get(f"/api/offers/check?offer={self.once.id}").data["code"], "ONCE20")
        self.assertEqual(self.client.get("/api/offers/check?offer=once20").data["id"], self.once.id)
        r = self.client.get("/api/offers/check?offer=NOPE")
        self.assertEqual((r.status_code, r.data["offer"]), (400, "There is no offer with that code."))
        self.assertEqual(self.client.get(f"/api/offers/check?offer={self.old.id}").data["offer"], "This offer has ended.")
        # Prices with ?offer= carry it; paying with it records it on the order.
        gold = next(p for p in self.client.get(f"/api/plans?offer={self.once.id}").data if p["tier"] == "premium")
        row = next(x for x in gold["prices"] if x["code"] == "premium-7d")
        self.assertEqual((row["offer"]["id"], row["price"]), (self.once.id, int(round(row["list_price"] * 0.8 / 100) * 100)))
        self.adv.diamonds = 10_000
        self.adv.save()
        r = self.client.post(f"/api/my-ads/{ad.id}/activate", {"product": "premium-7d", "method": "coins", "offer": "ONCE20"}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        order = Order.objects.get(order_id=r.data["order_id"])
        self.assertEqual((order.offer, order.amount), (self.once, row["price"]))
        # Once per advertiser: now refused.
        r = self.client.post(f"/api/my-ads/{ad.id}/activate", {"product": "premium-7d", "method": "coins", "offer": "ONCE20"}, format="json")
        self.assertEqual((r.status_code, r.data["offer"]), (400, "You have already used this offer."))


class MyAdsListTests(Base):
    """GET /api/my-ads – card fields: category names, "payment missing" flag, and live ads can't be deleted."""

    def test_card_fields_and_delete_rule(self):
        self.client.force_authenticate(self.adv)
        draft = self.make_ad(title="Draft", categories=["massage", "escort"])
        live = self.make_ad(title="Live")
        live.approve()
        rows = {a["title"]: a for a in self.client.get("/api/my-ads").data}
        self.assertEqual(rows["Draft"]["category_labels"], ["Massage", "Escort"])
        self.assertEqual((rows["Draft"]["paid_once"], rows["Live"]["paid_once"]), (False, True))
        self.assertIn("pending_tier", rows["Draft"])
        self.assertEqual(self.client.delete(f"/api/my-ads/{live.id}").status_code, 403)
        self.assertEqual(self.client.delete(f"/api/my-ads/{draft.id}").status_code, 204)


from rest_framework.test import APITransactionTestCase


class RealtimeMessagingTests(APITransactionTestCase):
    """WebSocket events: new message (+ unread total), typing, read receipts and presence.

    A transaction test case, so on_commit pushes fire and the socket threads can see the data.
    """

    setUp = Base.setUp
    make_ad = Base.make_ad

    def test_socket_events(self):
        import asyncio

        from channels.db import database_sync_to_async
        from channels.testing import WebsocketCommunicator
        from django.test import override_settings

        from .realtime import MessagesConsumer, is_online

        ad = self.make_ad()
        ad.approve()

        def connect(user):
            comm = WebsocketCommunicator(MessagesConsumer.as_asgi(), "/ws/messages/")
            comm.scope["user"] = user
            return comm

        def api(user, method, url, data=None):
            self.client.force_authenticate(user)
            return getattr(self.client, method)(url, data, format="json")

        async def scenario():
            adv, vis = connect(self.adv), connect(self.visitor)
            self.assertTrue((await adv.connect())[0])
            # Visitor writes first → advertiser gets the message, the conversation row and unread total 1.
            res = await database_sync_to_async(api)(self.visitor, "post", f"/api/ads/{ad.id}/message", {"body": "Hi!"})
            cid = res.data["conversation_id"]
            event = await adv.receive_json_from(timeout=2)
            self.assertEqual((event["type"], event["message"]["body"], event["message"]["mine"], event["unread_total"]),
                             ("message", "Hi!", False, 1))
            self.assertEqual((event["conversation"]["unread"], event["conversation"]["other_name"] != ""), (1, True))

            # Visitor comes online → advertiser is told.
            self.assertTrue((await vis.connect())[0])
            event = await adv.receive_json_from(timeout=2)
            self.assertEqual((event["type"], event["online"]), ("presence", True))
            self.assertTrue(await database_sync_to_async(is_online)(self.visitor.pk))

            # Typing goes to the other person only.
            await vis.send_json_to({"type": "typing", "conversation_id": cid})
            self.assertEqual(await adv.receive_json_from(timeout=2), {"type": "typing", "conversation_id": cid})
            self.assertTrue(await vis.receive_nothing(timeout=0.2))

            # Advertiser opens the thread → visitor sees "read", advertiser's tabs get unread 0.
            await database_sync_to_async(api)(self.adv, "get", f"/api/conversations/{cid}")
            event = await vis.receive_json_from(timeout=2)
            self.assertEqual(event["type"], "read")
            self.assertIsNotNone(event["read_at"])
            self.assertEqual((await adv.receive_json_from(timeout=2))["unread_total"], 0)

            # Visitor leaves → advertiser sees them go offline.
            await vis.disconnect()
            event = await adv.receive_json_from(timeout=2)
            self.assertEqual((event["type"], event["online"]), ("presence", False))
            await adv.disconnect()

        with override_settings(CHANNEL_LAYERS={"default": {"BACKEND": "channels.layers.InMemoryChannelLayer"}}):
            asyncio.run(scenario())


class ChatMediaTests(Base):
    """Photos/videos, private file access (+ Range), edit, reactions, reply, unsend, remove for me, delete chat."""

    def setUp(self):
        import tempfile

        from django.test import override_settings
        super().setUp()
        self._media = override_settings(PRIVATE_MEDIA_ROOT=tempfile.mkdtemp())
        self._media.enable()
        self.addCleanup(self._media.disable)
        ad = self.make_ad()
        ad.approve()
        self.client.force_authenticate(self.visitor)
        self.cid = self.client.post(f"/api/ads/{ad.id}/message", {"body": "Hi"}, format="json").data["conversation_id"]
        self.url = f"/api/conversations/{self.cid}"

    def video(self, name="clip.mp4"):
        data = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 5000
        return SimpleUploadedFile(name, data, content_type="video/mp4")

    def test_media_flow(self):
        res = self.client.post(self.url, {"body": "", "files": [make_image(), self.video()]}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        msg = res.data
        self.assertEqual([a["kind"] for a in msg["attachments"]], ["image", "video"])
        photo, video = msg["attachments"]
        # Only participants can open files; videos support Range requests.
        self.assertEqual(self.client.get(photo["url"])["Content-Type"], "image/webp")
        ranged = self.client.get(video["url"], HTTP_RANGE="bytes=0-99")
        self.assertEqual((ranged.status_code, ranged["Content-Range"]), (206, f"bytes 0-99/{video['size']}"))
        outsider = User.objects.create_user("x@test.dk", "pw-Strong-123")
        self.client.force_authenticate(outsider)
        self.assertEqual(self.client.get(photo["url"]).status_code, 404)
        self.client.logout()
        self.assertIn(self.client.get(photo["url"]).status_code, (401, 403))

        # Inbox preview for a media-only message.
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.get("/api/conversations").data[0]["last_message"], "📎 2 files")

        # Bad files are refused.
        self.client.force_authenticate(self.visitor)
        bad = SimpleUploadedFile("x.mp4", b"not a video at all", content_type="video/mp4")
        self.assertEqual(self.client.post(self.url, {"files": [bad]}, format="multipart").status_code, 400)
        exe = SimpleUploadedFile("x.exe", b"MZ....", content_type="application/octet-stream")
        self.assertEqual(self.client.post(self.url, {"files": [exe]}, format="multipart").status_code, 400)

        # Unsend removes text, files and reactions for everyone.
        mid = msg["id"]
        self.assertEqual(self.client.delete(f"{self.url}/messages/{mid}?for=everyone").status_code, 204)
        self.client.force_authenticate(self.adv)
        gone = next(m for m in self.client.get(self.url).data["messages"] if m["id"] == mid)
        self.assertEqual((gone["deleted"], gone["attachments"], gone["body"]), (True, [], ""))
        self.assertEqual(self.client.get(photo["url"]).status_code, 404)

    def test_edit_react_reply_and_remove(self):
        hi = self.client.get(self.url).data["messages"][0]
        # Advertiser can't edit or unsend the visitor's message.
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.patch(f"{self.url}/messages/{hi['id']}", {"body": "x"}, format="json").status_code, 403)
        self.assertEqual(self.client.delete(f"{self.url}/messages/{hi['id']}?for=everyone").status_code, 403)
        # …but can react (same emoji again removes it) and reply.
        r = self.client.post(f"{self.url}/messages/{hi['id']}/react", {"emoji": "❤️"}, format="json").data
        self.assertEqual(r["reactions"], [{"emoji": "❤️", "mine": True}])
        r = self.client.post(f"{self.url}/messages/{hi['id']}/react", {"emoji": "❤️"}, format="json").data
        self.assertEqual(r["reactions"], [])
        self.assertEqual(self.client.post(f"{self.url}/messages/{hi['id']}/react", {"emoji": "🍕"}, format="json").status_code, 400)
        reply = self.client.post(self.url, {"body": "Hello!", "reply_to": hi["id"]}, format="json").data
        self.assertEqual(reply["reply_to"], {"id": hi["id"], "mine": False, "text": "Hi", "deleted": False})

        # Visitor edits their own message.
        self.client.force_authenticate(self.visitor)
        edited = self.client.patch(f"{self.url}/messages/{hi['id']}", {"body": "Hi there"}, format="json").data
        self.assertEqual((edited["body"], edited["edited"]), ("Hi there", True))

        # "Remove for me" hides it only for me.
        self.assertEqual(self.client.delete(f"{self.url}/messages/{reply['id']}?for=me").status_code, 204)
        self.assertNotIn(reply["id"], [m["id"] for m in self.client.get(self.url).data["messages"]])
        self.client.force_authenticate(self.adv)
        self.assertIn(reply["id"], [m["id"] for m in self.client.get(self.url).data["messages"]])

        # Deleting the chat hides old messages; a new message brings back only the new one.
        self.assertEqual(self.client.delete(self.url).status_code, 204)
        self.assertEqual(self.client.get("/api/conversations").data, [])
        self.client.force_authenticate(self.visitor)
        self.client.post(self.url, {"body": "Still there?"}, format="json")
        self.client.force_authenticate(self.adv)
        self.assertEqual([m["body"] for m in self.client.get(self.url).data["messages"]], ["Still there?"])


class NotificationTests(Base):
    def setUp(self):
        super().setUp()
        from core.models import Notification
        self.N = Notification
        self.ad = self.make_ad(title="Aurora")
        self.ad.approve()
        self.other = User.objects.create_user("o3@test.dk", "pw-Strong-123", display_name="Bea")
        self.visitor.display_name = "Anna"
        self.visitor.save()

    def mine(self, user=None):
        return list(self.N.objects.filter(user=user or self.adv).order_by("-updated_at", "-id"))

    def test_likes_are_grouped_and_own_like_is_ignored(self):
        for u in (self.visitor, self.other):
            self.client.force_authenticate(u)
            self.client.post(f"/api/ads/{self.ad.id}/heart")
        self.client.force_authenticate(self.adv)
        self.client.post(f"/api/ads/{self.ad.id}/heart")  # own like: no notification
        rows = self.mine()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].count, 2)
        res = self.client.get("/api/notifications")
        self.assertEqual(res.data["unread"], 1)
        self.assertEqual(res.data["results"][0]["message"], "Bea and 1 other liked your ad “Aurora”.")

    def test_favourite_is_anonymous(self):
        self.client.force_authenticate(self.visitor)
        self.client.post(f"/api/ads/{self.ad.id}/favorite")
        self.client.force_authenticate(self.adv)
        msg = self.client.get("/api/notifications").data["results"][0]["message"]
        self.assertIn("Someone saved your ad", msg)
        self.assertNotIn("Anna", msg)

    def test_review_payment_and_id(self):
        from core.models import IdVerification, Order, Product
        pending = self.make_ad(title="Second", status=Ad.Status.PENDING)
        pending.approve()
        rejected = self.make_ad(title="Third", status=Ad.Status.PENDING)
        rejected.status, rejected.rejection_reason = Ad.Status.REJECTED, "Blurry pictures"
        rejected.save()
        product = Product.objects.first()
        if product:
            order = Order.objects.create(user=self.adv, ad=pending, product=product, product_name="Premium 7 days",
                                         tier="premium", days=7, amount=19900, currency="DKK")
            order.status = Order.Status.PAID
            order.save()
            order.save()  # saving again must not notify twice
        v = IdVerification.objects.create(user=self.adv, document_type="passport", full_name="A", date_of_birth="1990-01-01")
        v.decide(True)
        kinds = [n.kind for n in self.mine()]
        self.assertIn("ad_approved", kinds)
        self.assertIn("ad_rejected", kinds)
        self.assertIn("id_approved", kinds)
        self.assertEqual(kinds.count("payment"), 1 if product else 0)
        self.assertEqual(self.N.objects.get(kind="ad_rejected").text, "Blurry pictures")

    def test_expiry_reminders_are_sent_once(self):
        from datetime import timedelta

        from django.utils import timezone

        from core.models import Favorite
        from core.notifications import sweep
        soon = self.make_ad(title="Soon")
        soon.approve()
        Ad.objects.filter(pk=soon.pk).update(expires_at=timezone.now() + timedelta(hours=20))
        Ad.objects.filter(pk=self.ad.pk).update(expires_at=timezone.now() - timedelta(hours=1))
        Favorite.objects.create(user=self.visitor, ad=self.ad)
        self.N.objects.all().delete()
        self.assertEqual(sweep(force=True), 3)  # expires soon + expired + favourite gone
        self.assertEqual(sweep(force=True), 0)
        self.assertEqual(sorted(n.kind for n in self.mine()), ["ad_expired", "ad_expiring"])
        self.assertEqual([n.kind for n in self.mine(self.visitor)], ["favorite_gone"])

    def test_read_unread_remove_and_privacy(self):
        self.client.force_authenticate(self.visitor)
        self.client.post(f"/api/ads/{self.ad.id}/heart")
        n = self.mine()[0]
        self.assertEqual(self.client.post(f"/api/notifications/{n.id}").status_code, 404)  # not the visitor's
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.get("/api/notifications/unread").data["unread"], 1)
        self.assertEqual(self.client.post(f"/api/notifications/{n.id}").data["unread"], 0)
        self.assertEqual(self.client.post(f"/api/notifications/{n.id}", {"read": False}, format="json").data["unread"], 1)
        self.assertEqual(self.client.post("/api/notifications/read-all").data["unread"], 0)
        self.assertEqual(self.client.get("/api/notifications?filter=unread").data["results"], [])
        # A like after reading starts a new notification instead of bumping the read one.
        self.client.force_authenticate(self.other)
        self.client.post(f"/api/ads/{self.ad.id}/heart")
        self.assertEqual(len(self.mine()), 2)
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.delete(f"/api/notifications/{n.id}").status_code, 200)
        self.assertEqual(len(self.mine()), 1)


class WorldSeedTests(APITestCase):
    def test_seed_adds_inactive_areas_and_cities_that_can_be_switched_on(self):
        from django.core.management import call_command

        from .management.commands.seed_world import activate_country
        from .models import AdPlace, AdRegion, Country
        call_command("seed_world", stdout=io.StringIO())
        self.assertGreater(Country.objects.count(), 240)
        india = AdRegion.objects.filter(country__code="IN")
        self.assertTrue(india.exists())
        self.assertFalse(india.filter(is_active=True).exists())  # new areas start hidden
        self.assertFalse(any(r["country"] == "IN" for r in self.client.get("/api/meta").data["regions"]))
        # Nepal keeps its hand-made areas only.
        self.assertFalse(AdRegion.objects.filter(country__code="NP").exclude(geo_code="").exists())
        activate_country("IN")
        meta = self.client.get("/api/meta").data
        self.assertTrue(any(r["country"] == "IN" for r in meta["regions"]))
        mumbai = AdPlace.objects.get(region__country__code="IN", name="Mumbai")
        self.assertIn(mumbai.slug, [p["value"] for p in meta["places"]])
        # Running it again adds nothing.
        out = io.StringIO()
        call_command("seed_world", stdout=out)
        self.assertIn("Added 0 countries, 0 regions, 0 cities", out.getvalue())


def make_mp4(seconds: float, timescale: int = 1000) -> bytes:
    """Tiny MP4: an ftyp box and a moov box with a movie header (mvhd) of the given length."""
    import struct
    ftyp = struct.pack(">I4s4sI4s", 20, b"ftyp", b"isom", 0, b"isom")
    mvhd_body = bytes(4) + bytes(8) + struct.pack(">II", timescale, int(seconds * timescale)) + bytes(80)
    mvhd = struct.pack(">I4s", 8 + len(mvhd_body), b"mvhd") + mvhd_body
    moov = struct.pack(">I4s", 8 + len(mvhd), b"moov") + mvhd
    mdat = struct.pack(">I4s", 8 + 2000, b"mdat") + bytes(2000)
    return ftyp + mdat + moov  # moov after the data, like most phone videos


class VipTests(Base):
    """Visitor Free/VIP: what Free visitors can't do, buying VIP, ad videos (30 s) and reviews."""

    def setUp(self):
        import tempfile

        from django.test import override_settings
        super().setUp()
        self._media = override_settings(MEDIA_ROOT=tempfile.mkdtemp())
        self._media.enable()
        self.addCleanup(self._media.disable)
        self.free = User.objects.create_user("free@test.dk", "pw-Strong-123")
        self.ad = self.make_ad()
        self.ad.approve()

    def test_free_visitor_is_stopped_with_vip_flag(self):
        self.client.force_authenticate(self.free)
        for url, body in ((f"/api/ads/{self.ad.id}/message", {"body": "Hi"}),
                          (f"/api/ads/{self.ad.id}/comments", {"body": "Nice"}),
                          (f"/api/ads/{self.ad.id}/reviews", {"rating": 5, "body": "Great"})):
            res = self.client.post(url, body, format="json")
            self.assertEqual(res.status_code, 403, url)
            self.assertTrue(res.data["vip_required"], url)
        # Favourites stay free.
        self.assertEqual(self.client.post(f"/api/ads/{self.ad.id}/favorite").status_code, 200)

    def test_advertiser_can_answer_after_visitor_vip_ends(self):
        self.client.force_authenticate(self.visitor)
        cid = self.client.post(f"/api/ads/{self.ad.id}/message", {"body": "Hi"}, format="json").data["conversation_id"]
        User.objects.filter(pk=self.visitor.pk).update(vip_until=timezone.now() - timedelta(minutes=1))
        self.visitor.refresh_from_db()
        self.assertEqual(self.client.post(f"/api/conversations/{cid}", {"body": "again"}, format="json").status_code, 403)
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.post(f"/api/conversations/{cid}", {"body": "Hello!"}, format="json").status_code, 201)
        # The owner can always comment on their own ad.
        self.assertEqual(self.client.post(f"/api/ads/{self.ad.id}/comments", {"body": "Welcome"}, format="json").status_code, 201)

    def test_buy_vip_with_coins_adds_days(self):
        self.free.diamonds = 100
        self.free.save()
        self.client.force_authenticate(self.free)
        data = self.client.get("/api/vip").data
        month = next(p for p in data["prices"] if p["days"] == 30)
        self.assertFalse(data["status"]["is_vip"])
        res = self.client.post("/api/vip", {"product": month["code"], "method": "coins"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertTrue(res.data["is_vip"])
        self.free.refresh_from_db()
        self.assertEqual(self.free.diamonds, 100 - month["coins"])
        first_end = self.free.vip_until
        self.client.post("/api/vip", {"product": month["code"], "method": "coins"}, format="json")
        self.free.refresh_from_db()
        self.assertAlmostEqual((self.free.vip_until - first_end).days, 30, delta=1)  # days add up
        self.free.diamonds = 0
        self.free.save()
        self.assertEqual(self.client.post("/api/vip", {"product": month["code"], "method": "coins"}, format="json").status_code, 400)

    def test_buy_vip_by_card(self):
        from .models import Order
        from .payments import apply_gateway_state
        self.client.force_authenticate(self.free)
        code = self.client.get("/api/vip").data["prices"][0]["code"]
        res = self.client.post("/api/vip", {"product": code, "method": "card"}, format="json")
        self.assertEqual(res.status_code, 201, res.data)
        order = Order.objects.get(order_id=res.data["order_id"])
        self.assertIsNone(order.ad)
        apply_gateway_state(order.pk, {"accepted": True, "order_id": order.order_id, "currency": order.currency})
        apply_gateway_state(order.pk, {"accepted": True, "order_id": order.order_id, "currency": order.currency})
        self.free.refresh_from_db()
        self.assertTrue(self.free.is_vip)
        self.assertAlmostEqual((self.free.vip_until - timezone.now()).days, order.days, delta=1)  # given once

    def test_video_upload_length_and_vip_only(self):
        from django.core.files.uploadedfile import SimpleUploadedFile
        url = f"/api/my-ads/{self.ad.id}/videos"
        self.client.force_authenticate(self.adv)
        long = SimpleUploadedFile("long.mp4", make_mp4(45), content_type="video/mp4")
        res = self.client.post(url, {"file": long}, format="multipart")
        self.assertEqual(res.status_code, 400)
        self.assertIn("30 seconds", str(res.data))
        bad = SimpleUploadedFile("x.mp4", b"not a video at all, not even close", content_type="video/mp4")
        self.assertEqual(self.client.post(url, {"file": bad}, format="multipart").status_code, 400)
        ok = SimpleUploadedFile("ok.mov", make_mp4(29.6), content_type="video/quicktime")
        res = self.client.post(url, {"file": ok}, format="multipart")
        self.assertEqual(res.status_code, 201, res.data)
        self.assertEqual(len(res.data["videos"]), 1)
        self.assertAlmostEqual(res.data["videos"][0]["duration"], 29.6, delta=0.1)
        # Free visitors see a locked video, VIPs get the file.
        self.client.force_authenticate(self.free)
        v = self.client.get(f"/api/ads/{self.ad.id}").data["videos"][0]
        self.assertTrue(v["locked"])
        self.assertIsNone(v["url"])
        self.client.force_authenticate(self.visitor)
        v = self.client.get(f"/api/ads/{self.ad.id}").data["videos"][0]
        self.assertFalse(v["locked"])
        self.assertTrue(v["url"].endswith(".mov"))
        # Owner removes it.
        self.client.force_authenticate(self.adv)
        res = self.client.delete(f"/api/my-ads/{self.ad.id}/videos/{v['id']}")
        self.assertEqual(res.data["videos"], [])

    def test_reviews(self):
        url = f"/api/ads/{self.ad.id}/reviews"
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.post(url, {"rating": 6}, format="json").status_code, 400)
        self.assertEqual(self.client.post(url, {"rating": 4, "body": "Good"}, format="json").status_code, 201)
        res = self.client.post(url, {"rating": 5, "body": "Even better"}, format="json")  # updates, not a second one
        self.assertEqual(res.status_code, 200)
        self.assertEqual(self.client.get(f"/api/ads/{self.ad.id}").data["rating"], {"average": 5.0, "count": 1})
        self.client.force_authenticate(self.adv)
        self.assertEqual(self.client.post(url, {"rating": 5}, format="json").status_code, 400)  # own ad
        self.assertEqual(self.client.delete(f"/api/reviews/{res.data['id']}").status_code, 403)
        self.client.force_authenticate(self.visitor)
        self.assertEqual(self.client.delete(f"/api/reviews/{res.data['id']}").status_code, 204)


# ---------------------------------------------------------------------------
# E-mails
# ---------------------------------------------------------------------------
from django.core import mail


@override_settings(EMAIL_BRAND_NAME="KingOfBabes", DEFAULT_FROM_EMAIL="no-reply@kingofbabes.test",
                   PUBLIC_SITE_URL="https://kingofbabes.test")
class EmailTests(Base):
    def setUp(self):
        from django.core.cache import cache
        super().setUp()
        cache.clear()  # auth endpoints are rate-limited

    def last(self):
        return mail.outbox[-1]

    def test_signup_sends_branded_welcome_with_confirm_link(self):
        mail.outbox.clear()
        r = self.client.post("/api/auth/signup", {"email": "new@test.dk", "password": "pw-Strong-123",
                                                   "display_name": "New", "confirm_adult": True, "accept_terms": True}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        m = self.last()
        self.assertEqual(m.from_email, "KingOfBabes <no-reply@kingofbabes.test>")
        self.assertIn("confirm", m.subject.lower())
        self.assertIn("https://kingofbabes.test/confirm-email?token=", m.body)
        html = m.alternatives[0][0]
        self.assertIn("KingOfBabes", html)
        self.assertIn("Confirm my e-mail", html)

    def test_forgot_and_reset_password(self):
        mail.outbox.clear()
        self.client.post("/api/auth/forgot-password", {"email": "nobody@test.dk"}, format="json")
        self.assertEqual(len(mail.outbox), 0)  # same answer, no e-mail for unknown addresses
        r = self.client.post("/api/auth/forgot-password", {"email": "ADV@test.dk"}, format="json")
        self.assertEqual(r.status_code, 200)
        body = self.last().body
        self.assertIn("reset your password", self.last().subject)
        from urllib.parse import parse_qs, urlparse
        q = parse_qs(urlparse(body[body.index("https://kingofbabes.test/reset-password"):].split()[0]).query)
        uid, token = q["uid"][0], q["token"][0]
        self.assertTrue(self.client.post("/api/auth/reset-password", {"uid": uid, "token": token}, format="json").data["valid"])
        self.assertEqual(self.client.post("/api/auth/reset-password", {"uid": uid, "token": token, "new_password": "123"},
                                          format="json").status_code, 400)
        r = self.client.post("/api/auth/reset-password", {"uid": uid, "token": token, "new_password": "New-Strong-456"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.adv.refresh_from_db()
        self.assertTrue(self.adv.check_password("New-Strong-456"))
        self.assertIn("password was changed", self.last().subject)
        # The link only works once.
        self.assertEqual(self.client.post("/api/auth/reset-password", {"uid": uid, "token": token}, format="json").status_code, 400)

    def test_login_alert_only_on_new_browser(self):
        mail.outbox.clear()
        self.client.post("/api/auth/login", {"email": "adv@test.dk", "password": "pw-Strong-123"}, format="json")
        self.assertIn("new sign-in", self.last().subject)
        n = len(mail.outbox)
        self.client.post("/api/auth/logout")
        self.client.post("/api/auth/login", {"email": "adv@test.dk", "password": "pw-Strong-123"}, format="json")
        self.assertEqual(len(mail.outbox), n)  # same browser (cookie) → no second alert

    def test_change_email_tells_old_address(self):
        self.client.force_authenticate(self.adv)
        mail.outbox.clear()
        r = self.client.post("/api/account/email-change", {"email": "moved@test.dk", "password": "pw-Strong-123"}, format="json")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertIn("adv@test.dk", [m.to[0] for m in mail.outbox])
        self.assertIn("moved@test.dk", [m.to[0] for m in mail.outbox])  # confirmation for the new one

    def test_events_send_emails(self):
        from .models import IdVerification
        mail.outbox.clear()
        ad = self.make_ad(status=Ad.Status.PENDING)
        ad.status = Ad.Status.ACTIVE
        ad.save()
        self.assertIn("your ad is live", self.last().subject)
        v = IdVerification.objects.create(user=self.adv, document_type="passport", full_name="A B", date_of_birth="1990-01-01")
        v.decide(False, reason="Blurry picture")
        self.assertIn("not approved", self.last().subject)
        self.assertIn("Blurry picture", self.last().body)
        # VIP bought with coins
        product = Product.objects.filter(kind=Product.Kind.MEMBERSHIP).first()
        self.visitor.diamonds = 100
        self.visitor.save()
        from . import membership
        membership.buy_with_coins(self.visitor, product)
        self.assertIn("welcome to VIP", self.last().subject)
        self.assertEqual(self.last().to, [self.visitor.email])

    def test_discreet_sender_hides_brand(self):
        self.adv.sender_name = User.SenderName.NEUTRAL
        self.adv.save()
        mail.outbox.clear()
        ad = self.make_ad(status=Ad.Status.PENDING)
        ad.status = Ad.Status.ACTIVE
        ad.save()
        m = self.last()
        self.assertTrue(m.subject.startswith("Notification:"))
        self.assertNotIn("KingOfBabes", m.from_email + m.body + m.alternatives[0][0])


@override_settings(BTPANEL_MAIL_URL="https://panel.test/mail_sys/send_mail_http.json", BTPANEL_MAIL_FROM="info@site.test",
                   BTPANEL_MAIL_PASSWORD="pw", EMAIL_BRAND_NAME="KingOfBabes")
class BTPanelMailTests(APITestCase):
    def test_sends_html_through_the_panel_api(self):
        from django.core.mail import get_connection

        from . import emails, mail_backends
        posted = []
        with mock.patch.object(mail_backends.BTPanelEmailBackend, "_post", lambda self, data: posted.append(data) or {"status": True}):
            with override_settings(EMAIL_BACKEND="core.mail_backends.BTPanelEmailBackend"):
                emails.send(to="a@test.dk", subject="hello", heading="Hello", lines=["Hi"], background=False, fail_silently=False)
        self.assertEqual(len(posted), 1)
        d = posted[0]
        self.assertEqual((d["mail_from"], d["password"], d["mail_to"], d["subtype"]), ("info@site.test", "pw", "a@test.dk", "html"))
        self.assertEqual(d["subject"], "KingOfBabes: hello")
        self.assertIn("<h1", d["content"])
        # A refusal from the panel is reported as an error.
        with mock.patch.object(mail_backends.BTPanelEmailBackend, "_post", lambda self, data: {"status": False, "msg": "bad password"}):
            with self.assertRaises(mail_backends.BTPanelMailError):
                get_connection("core.mail_backends.BTPanelEmailBackend").send_messages([mock.Mock(
                    alternatives=[], recipients=lambda: ["a@test.dk"], subject="s", body="b")])


@override_settings(DEFAULT_FROM_EMAIL="info@escortarea.test", EMAIL_BRAND_NAME="EscortArea")
class SenderAddressTests(APITestCase):
    def test_info_sends_support_receives(self):
        from django.core.cache import cache

        from .models import SiteSettings
        cache.clear()
        s = SiteSettings.load()
        s.contact_email = "support@escortarea.test"
        s.save()
        user = User.objects.create_user("u@test.dk", "pw-Strong-123")
        mail.outbox.clear()
        self.client.post("/api/auth/forgot-password", {"email": "u@test.dk"}, format="json")
        m = mail.outbox[-1]
        self.assertEqual((m.from_email, m.reply_to), ("EscortArea <info@escortarea.test>", ["support@escortarea.test"]))
        self.assertEqual(m.to, [user.email])
        # The contact form goes to support, and replying answers the visitor.
        r = self.client.post("/api/contact", {"topic": "general", "name": "Ann", "email": "ann@test.dk",
                                               "message": "Hello, I have a question."}, format="json")
        self.assertEqual(r.status_code, 201, r.data)
        m = mail.outbox[-1]
        self.assertEqual((m.to, m.reply_to, m.from_email), (["support@escortarea.test"], ["ann@test.dk"], "EscortArea <info@escortarea.test>"))


from . import legacy as legacy_mod  # noqa: E402
from .models import LegacyAccount  # noqa: E402


class EscortsideImportTests(Base):
    USERS = [
        {"id": "10", "username": "Anna", "email": "anna@old.dk", "contact_country_code": "+45", "contact": "12345678",
         "is_profile_verified": "True", "age": "27", "title": "Sweet Anna", "areas": "København, Odense",
         "is_active": "True", "is_staff": "False", "is_deleted": "False", "date_joined": "2025-01-02 10:00:00+00:00"},
        {"id": "11", "username": "Anna2", "email": "ANNA@old.dk", "contact_country_code": "+45", "contact": "",
         "is_profile_verified": "False", "age": "", "title": "", "areas": "Hele Danmark",
         "is_active": "False", "is_staff": "False", "is_deleted": "False", "date_joined": ""},
        {"id": "12", "username": "gone", "email": "gone@old.dk", "is_deleted": "True", "is_staff": "False"},
        {"id": "13", "username": "boss", "email": "boss@old.dk", "is_deleted": "False", "is_staff": "True"},
    ]
    ADS = [{"id": "500", "user": "Anna", "ad_type": "Free"}]

    def run_import(self):
        return legacy_mod.import_rows(self.USERS, self.ADS)

    def test_import_creates_advertisers_with_free_draft_ads(self):
        stats = self.run_import()
        self.assertEqual((stats["users_created"], stats["users_merged"], stats["ads_created"]), (1, 1, 2))
        self.assertEqual(len(stats["skipped"]), 2)  # deleted + staff
        user = User.objects.get(email="anna@old.dk")
        self.assertTrue(user.is_advertiser and user.age_verified)
        self.assertFalse(user.has_usable_password())
        self.assertEqual(user.ads.count(), 2)
        self.assertTrue(all(a.status == "draft" and a.tier == "free" for a in user.ads.all()))
        acc = LegacyAccount.objects.get(legacy_id=10)
        self.assertEqual((acc.ad.title, acc.ad.age, acc.legacy_ad_id), ("Sweet Anna", 27, 500))
        self.assertEqual(self.run_import()["ads_created"], 0)  # running again adds nothing

    def test_import_maps_ticks_media_and_goes_live(self):
        import tempfile
        from pathlib import Path

        from PIL import Image

        from core.models import AdCategory, ChoiceOption

        row = dict(self.USERS[0], bio="Hello", escort="True", lingerie_sale="True", woman="True", men="True",
                   tran_s="True", anal="True", happy_ending="True", incalls="True", handicap_friendly_yes="True")
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp) / "10_Anna" / "images"
            folder.mkdir(parents=True)
            for name in ("photo_2_a.jpg", "profile_1_b.jpg"):
                Image.new("RGB", (40, 30), "red" if "photo" in name else "blue").save(folder / name)
            (folder / "photo_3_copy.jpg").write_bytes((folder / "photo_2_a.jpg").read_bytes())  # duplicate
            (folder / "photo_4_broken.png").write_bytes(b"not a picture")
            stats = legacy_mod.import_rows([row], self.ADS, user_media=tmp)
        ad = LegacyAccount.objects.get(legacy_id=10).ad
        self.assertEqual((ad.status, ad.tier, ad.free_listing, ad.expires_at.year), ("active", "free", True, 2100))
        self.assertEqual((ad.pictures.count(), stats["pictures"], len(stats["problems"])), (2, 2, 1))
        self.assertEqual((ad.pictures.first().width, ad.pictures.first().height), (40, 30))
        self.assertEqual((ad.categories, ad.gender, ad.audiences), (["escort", "lingerie-sale"], "female", ["men", "trans"]))
        self.assertEqual([s["name"] for s in ad.services], ["Anal", "Happy ending"])
        self.assertEqual((ad.accessibility, ad.description, ad.regions), ("yes", "Hello", ["copenhagen", "funen"]))
        self.assertEqual(list(ad.tags.values_list("slug", flat=True)), ["incall"])
        self.assertTrue(AdCategory.objects.filter(slug="lingerie-sale").exists())
        self.assertTrue(ChoiceOption.objects.filter(group="service", label="Happy ending").exists())
        self.assertTrue(ChoiceOption.objects.filter(group="audience", value="trans").exists())

    def test_no_area_uses_phone_country_and_wipe_keeps_admins(self):
        from core.models import AdRegion, Country

        se, _ = Country.objects.get_or_create(code="SE", defaults={"name": "Sweden", "dial_code": "+46"})
        AdRegion.objects.create(country=se, slug="se-stockholm", name="Stockholm")
        row = dict(self.USERS[0], areas="", contact_country_code="+46")
        legacy_mod.import_rows([row], [])
        ad = LegacyAccount.objects.get(legacy_id=10).ad
        self.assertEqual((ad.regions, ad.status), (["se-stockholm"], "draft"))  # no pictures: stays a draft
        se.refresh_from_db()
        self.assertTrue(se.has_ads)
        admin = User.objects.create_superuser(email="root@example.com", password="x-Strong-pw-1")
        gone = legacy_mod.wipe_ads_and_users()
        self.assertGreaterEqual(gone["ads"], 1)
        self.assertTrue(User.objects.filter(pk=admin.pk).exists())
        self.assertFalse(User.objects.filter(email="anna@old.dk").exists())

    def test_regions(self):
        self.assertEqual(legacy_mod.regions_for("København, Odense"), ["copenhagen", "funen"])
        self.assertEqual(len(legacy_mod.regions_for("Hele Danmark")), 10)
        self.assertEqual(legacy_mod.regions_for(""), [])

    def test_reward_steps(self):
        self.run_import()
        acc = LegacyAccount.objects.get(legacy_id=10)
        now = timezone.now()
        for days, tier in [(0, "vip"), (29, "vip"), (31, "premium"), (59, "premium"), (61, "standard"), (95, "free")]:
            acc.invited_at = now - timedelta(days=days)
            self.assertEqual(legacy_mod.reward_for(acc, now)[0], tier, days)

    def claim(self, **data):
        self.client.force_authenticate(None)
        return self.client.post("/api/auth/claim", data, format="json")

    def test_claim_gives_plan_and_signs_in(self):
        self.run_import()
        acc = LegacyAccount.objects.get(legacy_id=10)
        acc.invited_at = timezone.now() - timedelta(days=40)
        acc.save()
        token = legacy_mod.claim_token(acc)
        info = self.claim(token=token).data
        self.assertEqual((info["reward"]["tier"], info["ads"], info["claimed"]), ("premium", 2, False))
        self.assertEqual(self.claim(token=token, password="New-Strong-pw-77").status_code, 400)  # 18+ not ticked
        res = self.claim(token=token, password="New-Strong-pw-77", adult=True)
        self.assertEqual(res.status_code, 200, res.data)
        user = User.objects.get(email="anna@old.dk")
        self.assertTrue(user.check_password("New-Strong-pw-77") and user.email_confirmed)
        self.assertTrue(all(a.pending_tier == "premium" and a.pending_days == 60 for a in user.ads.all()))
        self.assertEqual(LegacyAccount.objects.filter(user=user, claimed_at__isnull=True).count(), 0)
        self.assertEqual(self.claim(token=token, password="Other-pw-99", adult=True).status_code, 400)  # only once
        ad = user.ads.first()
        ad.approve()
        self.assertEqual(ad.effective_tier, "premium")

    def test_bad_token_and_delete(self):
        self.run_import()
        self.assertEqual(self.claim(token="nope").status_code, 400)
        token = legacy_mod.claim_token(LegacyAccount.objects.get(legacy_id=10))
        self.assertTrue(self.claim(token=token, delete=True).data["deleted"])
        self.assertFalse(User.objects.filter(email="anna@old.dk").exists())

    def test_invite_email_test_mode_does_not_start_clock(self):
        from django.core import mail as django_mail

        self.run_import()
        acc = LegacyAccount.objects.get(legacy_id=10)
        legacy_mod.send_invite(acc, to="owner-test@example.com")
        acc.refresh_from_db()
        self.assertIsNone(acc.invited_at)
        self.assertEqual(django_mail.outbox[-1].to, ["owner-test@example.com"])
        self.assertIn("/welcome-back?token=", django_mail.outbox[-1].body)
        legacy_mod.send_invite(acc)
        acc.refresh_from_db()
        self.assertIsNotNone(acc.invited_at)
        self.assertEqual(django_mail.outbox[-1].to, ["anna@old.dk"])


class AdminDashboardTests(Base):
    def test_login_is_recorded_with_country_and_device(self):
        from core.models import LoginEvent

        res = self.client.post("/api/auth/login", {"email": "adv@test.dk", "password": "pw-Strong-123"}, format="json",
                               HTTP_CF_IPCOUNTRY="dk", HTTP_USER_AGENT="Mozilla/5.0 (Linux; Android 16) Chrome/157.0 Mobile")
        self.assertEqual(res.status_code, 200)
        e = LoginEvent.objects.get(user=self.adv)
        self.assertEqual((e.method, e.country, e.device), ("password", "DK", "Chrome · Android"))
        self.adv.refresh_from_db()
        self.assertIsNotNone(self.adv.last_login)

    def test_dashboard_lists_and_sidebar(self):
        from django.test import Client

        from core.models import IdVerification, LoginEvent

        ad = self.make_ad(title="Dash ad")
        ad.approve()
        IdVerification.objects.create(user=self.adv, document_type="passport", full_name="Anna Test",
                                      date_of_birth="1995-01-01")
        LoginEvent.objects.create(user=self.adv, method="password", country="DK", device="Chrome · Android")
        staff = User.objects.create_superuser("boss@test.dk", "pw-Strong-123")
        web = Client()
        web.force_login(staff)
        res = web.get("/admin/")
        self.assertEqual(res.status_code, 200)
        html = res.content.decode()
        for text in ("Waiting for ID verification", "Anna Test", "Dash ad", "Creators with ads", "Latest 10 users",
                     "Latest 10 advertisers", "Recent sign-ins", "Chrome · Android"):
            self.assertIn(text, html)
        sections = [a["name"] for a in res.context["available_apps"]]
        self.assertEqual(sections, ["Moderation", "People", "Sales", "Content", "Site setup"])
        menu = [m["object_name"] for a in res.context["available_apps"] for m in a["models"]]
        self.assertNotIn("Heart", menu)
        self.assertNotIn("Conversation", menu)
        id_entry = next(m for a in res.context["available_apps"] for m in a["models"] if m["object_name"] == "IdVerification")
        self.assertIn("(1)", id_entry["name"])  # waiting requests counted in the menu
        for url in ("/admin/core/user/", f"/admin/core/user/{self.adv.pk}/change/", "/admin/core/loginevent/"):
            self.assertEqual(web.get(url).status_code, 200, url)
