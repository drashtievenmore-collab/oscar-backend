"""Textile masters -- fabric quality, design, shade and fabric type."""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.accounts.authentication import build_tokens
from apps.accounts.models import Client, User
from apps.core.models import File

from .models import FabricQuality, FabricType


class TextileMasterTests(TestCase):
    def setUp(self):
        self.client_obj, _ = Client.objects.get_or_create(
            slug="textile-test-tenant", defaults={"name": "Textile Test Tenant"}
        )
        self.user = User.objects.create_user(
            email="textile-masters@example.com", password="pass", client=self.client_obj
        )
        self.api = APIClient()
        self.api.credentials(
            HTTP_AUTHORIZATION=f"Bearer {build_tokens(self.user)['access']}"
        )

    def _post(self, path, data):
        return self.api.post(f"/api/v1/inventory/{path}/", data, format="json")

    def test_code_is_derived_from_name(self):
        response = self._post("designs", {"name": "Paisley Bloom", "patternType": "Printed"})
        self.assertEqual(response.status_code, 201, response.content[:500])
        self.assertEqual(response.json()["code"], "PAISLEY-BLOOM")

    def test_duplicate_name_and_code_are_rejected(self):
        self.assertEqual(self._post("shades", {"name": "Navy", "code": "NV"}).status_code, 201)
        self.assertEqual(self._post("shades", {"name": "navy"}).status_code, 400)
        self.assertEqual(self._post("shades", {"name": "Navy Blue", "code": "nv"}).status_code, 400)

    def test_shade_hex_must_be_rrggbb(self):
        self.assertEqual(self._post("shades", {"name": "Red", "hexColor": "red"}).status_code, 400)
        response = self._post("shades", {"name": "Red", "hexColor": "#FF0000"})
        self.assertEqual(response.status_code, 201, response.content[:500])
        self.assertEqual(response.json()["hexColor"], "#FF0000")

    def test_shade_colour_type_and_pantone(self):
        response = self._post(
            "shades",
            {
                "name": "Navy Blue",
                "code": "SH-001",
                "colourType": "Solid",
                "pantoneReference": "PMS 296 C",
                "hexColor": "#0A192F",
            },
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        data = response.json()
        self.assertEqual(data["code"], "SH-001")
        self.assertEqual(data["colourType"], "Solid")
        self.assertEqual(data["pantoneReference"], "PMS 296 C")
        self.assertEqual(data["hexColor"], "#0A192F")

    def test_quality_links_fabric_type_and_specs(self):
        grey = self._post("fabric-types", {"name": "Grey"}).json()
        meter = self._post("units", {"code": "Meter", "label": "Meter"}).json()
        response = self._post(
            "fabric-qualities",
            {
                "name": "60x60 Cambric",
                "code": "COT-60S",
                "fabricTypeId": grey["id"],
                "composition": "100% Cotton",
                "uomId": meter["id"],
                "width": "58",
                "gsm": "110",
            },
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        body = response.json()
        self.assertEqual(body["fabricTypeName"], "Grey")
        self.assertEqual(body["composition"], "100% Cotton")
        self.assertEqual(body["uom"], "Meter")
        self.assertEqual(float(body["width"]), 58.0)
        self.assertEqual(
            FabricQuality.objects.get(id=body["id"]).fabric_type_id, FabricType.objects.get(name="Grey").id
        )

        # Verify search by composition works
        search_res = self.api.get("/api/v1/inventory/fabric-qualities/?q=Cotton")
        self.assertEqual(search_res.status_code, 200)
        self.assertEqual(len(search_res.json().get("results", [])), 1)

    def test_fabric_type_in_use_cannot_be_deleted(self):
        grey = self._post("fabric-types", {"name": "Grey"}).json()
        self._post("fabric-qualities", {"name": "Cambric", "fabricTypeId": grey["id"]})
        response = self.api.delete(f"/api/v1/inventory/fabric-types/{grey['id']}/")
        self.assertEqual(response.status_code, 409, response.content[:500])

    def test_edit_with_blank_code_keeps_code(self):
        created = self._post("designs", {"name": "Floral", "code": "FL-01", "patternType": "Printed"}).json()
        response = self.api.patch(
            f"/api/v1/inventory/designs/{created['id']}/", {"name": "Floral Big", "code": ""}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertEqual(response.json()["code"], "FL-01")

    def test_design_requires_pattern_type(self):
        response = self._post("designs", {"name": "Plain"})
        self.assertEqual(response.status_code, 400, response.content[:500])
        self.assertEqual(self._post("designs", {"name": "Plain", "patternType": "Wavy"}).status_code, 400)

    def test_design_season_reference_and_image(self):
        image = File.objects.create(
            client=self.client_obj, scope="other", storage_key="test/floral.png",
            file_name="floral.png", content_type="image/png", status="committed",
        )
        response = self._post(
            "designs",
            {
                "name": "Floral Print", "code": "D-001", "patternType": "Printed",
                "season": "Summer 2025", "reference": "BUYER-778", "imageFileId": str(image.id),
            },
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        body = response.json()
        self.assertEqual(body["patternType"], "Printed")
        self.assertEqual(body["season"], "Summer 2025")
        self.assertEqual(body["reference"], "BUYER-778")
        self.assertTrue(body["imageUrl"].startswith(f"/api/v1/files/{image.id}/download/?token="))

        listed_res = self.api.get("/api/v1/inventory/designs/?patternType=Printed")
        self.assertEqual(listed_res.status_code, 200, listed_res.content[:500])
        listed = listed_res.json().get("data") or listed_res.json().get("results", [])
        self.assertEqual([row["code"] for row in listed], ["D-001"])

    def test_design_image_from_another_tenant_is_rejected(self):
        other, _ = Client.objects.get_or_create(slug="other-tenant", defaults={"name": "Other"})
        foreign = File.objects.create(
            client=other, scope="other", storage_key="test/foreign.png",
            file_name="foreign.png", content_type="image/png", status="committed",
        )
        response = self._post(
            "designs", {"name": "Stolen", "patternType": "Solid", "imageFileId": str(foreign.id)}
        )
        self.assertEqual(response.status_code, 400, response.content[:500])

    def _variant_parts(self):
        meter = self._post("units", {"code": "Meter", "label": "Meter"}).json()
        quality = self._post(
            "fabric-qualities", {"name": "Cotton 60s", "code": "COT-60S", "uomId": meter["id"], "width": "58", "gsm": "120"}
        ).json()
        design = self._post("designs", {"name": "Floral Print", "code": "D-001", "patternType": "Printed"}).json()
        shade = self._post("shades", {"name": "Navy Blue", "code": "NAVY", "hexColor": "#1F3A93"}).json()
        return quality, design, shade

    def test_variant_derives_name_code_and_reads_quality_spec(self):
        quality, design, shade = self._variant_parts()
        response = self._post(
            "fabric-variants",
            {
                "qualityId": quality["id"], "designId": design["id"], "shadeId": shade["id"],
                "uom": "Meter", "fabricType": "Woven", "construction": "Plain",
                "rate": "145.50", "hsnCode": "5208",
            },
        )
        self.assertEqual(response.status_code, 201, response.content[:500])
        body = response.json()
        self.assertEqual(body["name"], "Cotton 60s – Floral Print – Navy Blue")
        self.assertEqual(body["code"], "COT-60S-D-001-NAVY")
        self.assertEqual(body["uom"], "Meter")
        self.assertEqual(float(body["width"]), 58.0)
        self.assertEqual(float(body["gsm"]), 120.0)
        self.assertEqual(body["shadeHex"], "#1F3A93")
        self.assertEqual(body["rate"], 145.5)
        self.assertEqual(body["fabricType"], "Woven")
        self.assertEqual(body["construction"], "Plain")

    def test_variant_quality_only_is_allowed_and_combination_is_unique(self):
        quality, design, _ = self._variant_parts()
        grey = self._post("fabric-variants", {"qualityId": quality["id"]})
        self.assertEqual(grey.status_code, 201, grey.content[:500])
        self.assertEqual(grey.json()["name"], "Cotton 60s")
        self.assertIsNone(grey.json()["designName"])

        first = self._post("fabric-variants", {"qualityId": quality["id"], "designId": design["id"], "code": "V1"})
        self.assertEqual(first.status_code, 201, first.content[:500])
        again = self._post("fabric-variants", {"qualityId": quality["id"], "designId": design["id"], "code": "V2", "name": "Other"})
        self.assertEqual(again.status_code, 400, again.content[:500])

    def test_variant_requires_quality(self):
        self.assertEqual(self._post("fabric-variants", {"name": "Loose"}).status_code, 400)

    def test_parts_in_use_by_variant_cannot_be_deleted(self):
        quality, design, shade = self._variant_parts()
        self._post("fabric-variants", {"qualityId": quality["id"], "designId": design["id"], "shadeId": shade["id"]})
        for path, row in (("fabric-qualities", quality), ("designs", design), ("shades", shade)):
            response = self.api.delete(f"/api/v1/inventory/{path}/{row['id']}/")
            self.assertEqual(response.status_code, 409, f"{path}: {response.content[:300]}")

    def test_variant_status_patch_keeps_parts(self):
        quality, design, shade = self._variant_parts()
        created = self._post(
            "fabric-variants", {"qualityId": quality["id"], "designId": design["id"], "shadeId": shade["id"]}
        ).json()
        response = self.api.patch(
            f"/api/v1/inventory/fabric-variants/{created['id']}/", {"isActive": False}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content[:500])
        self.assertFalse(response.json()["isActive"])
        self.assertEqual(response.json()["shadeName"], "Navy Blue")
