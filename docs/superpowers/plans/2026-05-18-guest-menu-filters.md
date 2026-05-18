# Guest Menu Filters Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move guest-menu tag visibility into lower-priority dedicated screens on web and iOS.

**Architecture:** Keep the existing tag visibility API and data model. Change only admin UI hierarchy, labels, and explanatory text.

**Tech Stack:** Python FastAPI single-file web app, vanilla HTML/CSS/JavaScript, SwiftUI iOS app.

---

### Task 1: Web Admin Page

**Files:**
- Modify: `/Users/petergu684/code/MealPlanner/meal-planner/server.py`

- [ ] Add a lower-priority "Guest Menu Filters" row on the home page.
- [ ] Add a `page-menu-filters` page with explanatory copy and the existing tag list container.
- [ ] Add a `navigate('menu-filters')` route that uses back navigation to Home and calls `loadTagManagement()`.
- [ ] Update `loadTagManagement()` copy and row markup so the toggle reads as guest filter visibility, not generic tag management.
- [ ] Remove `loadTagManagement()` from the home page loader.
- [ ] Verify `python3 -m py_compile server.py` succeeds.

### Task 2: iOS Settings Detail

**Files:**
- Modify: `/Users/petergu684/code/MealPlanner/meal-planner-ios/Meal Planner/ContentView.swift`
- Modify: `/Users/petergu684/code/MealPlanner/meal-planner-ios/Meal Planner/Localizable.xcstrings`

- [ ] Remove inline tag visibility rows from `SettingsView`.
- [ ] Add a Settings navigation row labeled "Guest Menu Filters" under the online Guest Menu section.
- [ ] Add a `GuestMenuFiltersView` SwiftUI detail screen with explanatory copy, loading state, empty state, tag counts, and visibility toggles.
- [ ] Keep the existing `updateTagVisibility` service call.
- [ ] Add Chinese localizations for new user-visible copy.
- [ ] Verify the project builds if Xcode tooling is available.
