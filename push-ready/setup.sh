#!/bin/bash
# HTCAH Clean Setup Script
# Run from your Howtocookathome folder: bash push-ready/setup.sh

set -e
echo "=== HTCAH Clean Setup ==="

# 1. Create LICENSE
echo "Creating LICENSE..."
cat > LICENSE << 'EOF'
Copyright (c) 2026 Matthew Mauer. All Rights Reserved.

PROPRIETARY SOFTWARE LICENSE

This software and associated documentation files (the "Software") are the
proprietary property of Matthew Mauer, operating as HowToCookAtHome.com.

TERMS:

1. NO DISTRIBUTION — The Software may not be copied, modified, merged,
   published, distributed, sublicensed, or sold without express written
   permission from the copyright holder.

2. NO REVERSE ENGINEERING — You may not decompile, disassemble, or
   reverse engineer any part of the Software.

3. CONTRIBUTOR TERMS — Any person granted access to this repository
   must sign the Contributor License Agreement (CLA.md) before
   submitting code. All contributions become the property of the
   copyright holder as described in the CLA.

4. USDA DATA — Nutrition data sourced from USDA FoodData Central is
   public domain and not subject to this license. See:
   https://fdc.nal.usda.gov/

5. THIRD-PARTY COMPONENTS — Third-party libraries used by this Software
   retain their original licenses. See requirements.txt for dependencies.

6. NO WARRANTY — THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF
   ANY KIND, EXPRESS OR IMPLIED.

For licensing inquiries: matt@howtocookathome.com
EOF

# 2. Create CLA
echo "Creating CLA.md..."
cat > CLA.md << 'EOF'
# Contributor License Agreement

## HowToCookAtHome.com — CLA v1.0 (April 2026)

By submitting code, documentation, designs, or other materials ("Contributions")
to this repository, you agree to the following terms:

### 1. IP Assignment

You assign all intellectual property rights in your Contributions to
Matthew Mauer ("Owner"). This includes copyright, patent rights, and
trade secrets. You retain the right to use your Contributions in other
projects, but you cannot revoke the assignment.

### 2. Original Work

You confirm that your Contributions are your original work, or that you
have the right to submit them. If your Contributions include third-party
code, you will clearly identify it and its license.

### 3. No Obligation

The Owner is not obligated to use your Contributions. Submitting a
Contribution does not create an employment or partnership relationship.

### 4. Confidentiality

You agree to keep proprietary information encountered in this repository
confidential. This includes business logic, pricing strategies, revenue
data, and unpublished features. Public-facing code (templates, APIs) is
not confidential once deployed.

### 5. How to Sign

Your first Pull Request to this repository serves as your signature.
By opening a PR, you confirm you have read and agree to this CLA.

---

Questions? Contact matt@howtocookathome.com
EOF

# 3. Create changelog workflow
echo "Creating GitHub Actions..."
mkdir -p .github/workflows
cat > .github/workflows/changelog.yml << 'EOF'
name: Auto-Changelog

on:
  push:
    branches: [main]

jobs:
  changelog:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
        with:
          fetch-depth: 0

      - name: Generate Changelog
        run: |
          echo "# HTCAH Changelog" > CHANGELOG.md
          echo "" >> CHANGELOG.md
          echo "Auto-generated from commit history." >> CHANGELOG.md
          echo "" >> CHANGELOG.md
          git log --format="- **%ad** — %s _(%an)_" --date=short >> CHANGELOG.md

      - name: Commit Changelog
        run: |
          git config user.name "github-actions[bot]"
          git config user.email "github-actions[bot]@users.noreply.github.com"
          git add CHANGELOG.md
          git diff --cached --quiet || git commit -m "Update changelog [skip ci]"
          git push
EOF

# 4. Remove demo tenants
echo "Removing demo tenants..."
for f in content/tenants/*.yaml; do
  slug=$(basename "$f" .yaml)
  if [ "$slug" != "htcah" ]; then
    rm "$f"
    echo "  Removed tenant: $slug"
  fi
done

# 5. Remove demo recipes (tenant-prefixed ones)
echo "Removing demo recipes..."
for slug in big-reds-bbq demo-taqueria mama-rosa-trattoria mikes-bbq-pit sarahs-bakery sarahs-bakery-v2 smash-stack-burgers smokehouse-joe test-kitchen tonys-pizza test-burger-joint; do
  count=$(ls content/recipes/${slug}-*.yaml 2>/dev/null | wc -l)
  rm -f content/recipes/${slug}-*.yaml
  if [ "$count" -gt 0 ]; then echo "  Removed $count recipes for $slug"; fi
done

# 6. Remove inventories
echo "Removing inventories..."
rm -f content/inventories/*.yaml

# 7. Update .gitignore
echo "Updating .gitignore..."
if ! grep -q "Business strategy docs" .gitignore 2>/dev/null; then
cat >> .gitignore << 'EOF'

# Business strategy docs (proprietary)
docs/*.xlsx
docs/original/
archive/
EOF
fi

# 8. Clean up
rm -rf session-fixes session-fixes.patch

# Report
echo ""
echo "=== DONE ==="
echo "Tenants: $(ls content/tenants/*.yaml | wc -l)"
echo "Recipes: $(ls content/recipes/*.yaml | wc -l)"
echo "LICENSE: $(test -f LICENSE && echo 'YES' || echo 'MISSING')"
echo "CLA.md: $(test -f CLA.md && echo 'YES' || echo 'MISSING')"
echo ""
echo "Now run:"
echo "  git add -A"
echo "  git status"
echo "  git commit -m 'Clean slate: remove demos, add LICENSE/CLA, fix routing'"
echo "  git push"
