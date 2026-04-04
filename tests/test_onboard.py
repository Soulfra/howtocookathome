"""Tests for the onboarding engine — dish generation, pricing, step generation."""
import os
import sys
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from app.onboard import slugify


class TestSlugify:
    def test_basic(self):
        assert slugify("Big Red's BBQ") == "big-reds-bbq"

    def test_spaces_and_special(self):
        assert slugify("Tony's Pizza & Pasta!") == "tonys-pizza-pasta"

    def test_already_slug(self):
        assert slugify("smash-stack-burgers") == "smash-stack-burgers"

    def test_unicode(self):
        result = slugify("La Cocina de Abuela")
        assert "la-cocina" in result


class TestDishGeneration:
    """Test that create_dishes_from_inventory produces sane output."""

    def _make_burger_inventory(self):
        return [
            {"item": "Ground Beef 80/20", "unit_price": 4.50, "unit": "lb", "category": "protein"},
            {"item": "American Cheese", "unit_price": 3.20, "unit": "lb", "category": "dairy"},
            {"item": "Brioche Buns", "unit_price": 0.45, "unit": "ea", "category": "bread"},
            {"item": "Bacon", "unit_price": 6.00, "unit": "lb", "category": "protein"},
            {"item": "Lettuce", "unit_price": 1.50, "unit": "lb", "category": "produce"},
            {"item": "Tomato", "unit_price": 2.00, "unit": "lb", "category": "produce"},
            {"item": "Onion", "unit_price": 1.00, "unit": "lb", "category": "produce"},
            {"item": "Pickles", "unit_price": 3.00, "unit": "gal", "category": "produce"},
            {"item": "Ketchup", "unit_price": 4.00, "unit": "gal", "category": "condiment"},
            {"item": "Mustard", "unit_price": 3.50, "unit": "gal", "category": "condiment"},
            {"item": "Salt", "unit_price": 1.00, "unit": "lb", "category": "spice"},
            {"item": "Black Pepper", "unit_price": 8.00, "unit": "lb", "category": "spice"},
            {"item": "Frozen Fries", "unit_price": 2.50, "unit": "lb", "category": "frozen"},
            {"item": "Fry Oil", "unit_price": 15.00, "unit": "gal", "category": "oil"},
            {"item": "Vanilla Custard Base", "unit_price": 8.00, "unit": "gal", "category": "dairy"},
        ]

    def test_burger_template_fires(self):
        from app.onboard import create_dishes_from_inventory
        result = create_dishes_from_inventory("test-burger-joint", self._make_burger_inventory())
        dishes = result[0] if isinstance(result, tuple) else result
        assert len(dishes) > 0
        names = [d["name"] for d in dishes]
        has_burger = any("burger" in n.lower() or "smash" in n.lower() for n in names)
        assert has_burger, f"Expected burger dishes, got: {names}"

    def test_prices_above_floor(self):
        from app.onboard import create_dishes_from_inventory
        result = create_dishes_from_inventory("test-burger-joint", self._make_burger_inventory())
        dishes = result[0] if isinstance(result, tuple) else result
        for d in dishes:
            price = d.get("suggested_menu_price", d.get("suggested_price", 0))
            name = d["name"].lower()
            if "fries" in name or "fry" in name:
                assert price >= 3.99, f"{d['name']} priced at ${price} (floor is $3.99)"
            if "burger" in name or "smash" in name:
                assert price >= 7.99, f"{d['name']} priced at ${price} (floor is $7.99)"

    def test_no_empty_ingredients(self):
        from app.onboard import create_dishes_from_inventory
        result = create_dishes_from_inventory("test-burger-joint", self._make_burger_inventory())
        dishes = result[0] if isinstance(result, tuple) else result
        for d in dishes:
            assert len(d.get("ingredients", [])) > 0, f"{d['name']} has no ingredients"

    def test_cooking_steps_are_specific(self):
        from app.onboard import create_dishes_from_inventory
        result = create_dishes_from_inventory("test-burger-joint", self._make_burger_inventory())
        dishes = result[0] if isinstance(result, tuple) else result
        for d in dishes:
            steps = d.get("steps", [])
            assert len(steps) > 0, f"{d['name']} has no cooking steps"
            if "burger" in d["name"].lower():
                step_text = " ".join(steps).lower()
                assert "smash" in step_text or "griddle" in step_text or "form" in step_text, \
                    f"Burger '{d['name']}' has non-burger steps: {steps[:2]}"
