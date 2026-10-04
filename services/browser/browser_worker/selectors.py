"""Every EspoCRM 10 UI selector the operator uses, in one place.

Grounded in accessibility snapshots of the real UI (login page, contact list,
contact create/edit/detail, task quick-create). Prefer role / label /
placeholder; fall back to EspoCRM's stable `data-name` / `data-action`
attributes (they are field and action names, not styling) where the UI gives
no accessible name (e.g. the email input has no label association).

F5 (UI-changed fault) works by breaking one of these, so keep them here.
"""

from __future__ import annotations

from playwright.async_api import Locator, Page

# ---- routes (hash router) -------------------------------------------------
ROUTE_HOME = "#"
ROUTE_CONTACTS = "#Contact"
ROUTE_CONTACT_CREATE = "#Contact/create"
ROUTE_ACCOUNTS = "#Account"
ROUTE_ACCOUNT_CREATE = "#Account/create"


def route_contact_view(contact_id: str) -> str:
    return f"#Contact/view/{contact_id}"


def route_contact_edit(contact_id: str) -> str:
    return f"#Contact/edit/{contact_id}"


# ---- login page -------------------------------------------------------------
def login_username(page: Page) -> Locator:
    return page.get_by_role("textbox", name="Username")


def login_password(page: Page) -> Locator:
    return page.get_by_role("textbox", name="Password")


def login_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Log in")


def login_error(page: Page) -> Locator:
    return page.locator(".login .alert-danger, #login .alert-danger, .alert-danger:has-text('Wrong')")


# ---- app shell ----------------------------------------------------------------
def app_navbar(page: Page) -> Locator:
    """The main navigation (banner > navigation); present only when logged in."""
    return page.get_by_role("banner").get_by_role("navigation")


def error_notice(page: Page) -> Locator:
    """Espo's floating notification for failed requests (e.g. 'Error 500')."""
    return page.locator("#notification .alert-danger, .notify .alert-danger")


def open_modal(page: Page) -> Locator:
    return page.locator(".modal-dialog:visible")


def page_heading(page: Page) -> Locator:
    return page.locator(".page-header h3, .header-title").first


# ---- list views -----------------------------------------------------------------
def list_text_filter(page: Page) -> Locator:
    return page.locator('input[data-name="textFilter"]')


def list_rows(page: Page) -> Locator:
    return page.locator(".list-container tr.list-row[data-id]")


def list_no_data(page: Page) -> Locator:
    return page.locator(".list-container .no-data")


def list_loaded(page: Page) -> Locator:
    """A rendered list: its first row or its empty marker."""
    return page.locator(".list-container tr.list-row[data-id], .list-container .no-data")


# ---- record forms (create / edit) -------------------------------------------------
def field_cell(scope: Page | Locator, name: str) -> Locator:
    return scope.locator(f'.cell[data-name="{name}"]')


def first_name(scope: Page | Locator) -> Locator:
    return scope.get_by_placeholder("First Name")


def last_name(scope: Page | Locator) -> Locator:
    return scope.get_by_placeholder("Last Name")


def email_inputs(scope: Page | Locator) -> Locator:
    return field_cell(scope, "emailAddress").locator("input.email-address")


def email_add_button(scope: Page | Locator) -> Locator:
    return field_cell(scope, "emailAddress").locator('button[data-action="addEmailAddress"]')


def phone_inputs(scope: Page | Locator) -> Locator:
    return field_cell(scope, "phoneNumber").locator("input.phone-number")


def phone_add_button(scope: Page | Locator) -> Locator:
    return field_cell(scope, "phoneNumber").locator('button[data-action="addPhoneNumber"]')


def accounts_select(scope: Page | Locator) -> Locator:
    return field_cell(scope, "accounts").get_by_placeholder("Select")


def account_title_inputs(scope: Page | Locator) -> Locator:
    """Per-account 'Title' (account role) inputs; appear once an account is linked."""
    return field_cell(scope, "accounts").get_by_placeholder("Title")


def assigned_user_input(scope: Page | Locator) -> Locator:
    return field_cell(scope, "assignedUser").locator('input[data-name="assignedUserName"]')


def assigned_user_id(scope: Page | Locator) -> Locator:
    return field_cell(scope, "assignedUser").locator('input[data-name="assignedUserId"]')


def name_input(scope: Page | Locator) -> Locator:
    """Single-value `name` field (Task, Account)."""
    return field_cell(scope, "name").locator('input[data-name="name"]')


def date_end_input(scope: Page | Locator) -> Locator:
    return field_cell(scope, "dateEnd").locator('input[data-name="dateEnd"]')


def parent_id_input(scope: Page | Locator) -> Locator:
    return field_cell(scope, "parent").locator('input[data-name="parentId"]')


def autocomplete_suggestions(page: Page) -> Locator:
    """Suggestions of the currently open autocomplete (appended to <body>)."""
    return page.locator(".autocomplete-suggestions:visible .autocomplete-suggestion")


def save_button(scope: Page | Locator) -> Locator:
    return scope.get_by_role("button", name="Save", exact=True).first


def edit_button(page: Page) -> Locator:
    return page.get_by_role("button", name="Edit", exact=True).first


# "The record you are creating might already exist" dialog on create.
DUPLICATE_DIALOG_HEADING = "might already exist"


def duplicate_dialog(page: Page) -> Locator:
    return page.get_by_role("dialog").filter(has_text=DUPLICATE_DIALOG_HEADING)


def duplicate_dialog_create(page: Page) -> Locator:
    return duplicate_dialog(page).get_by_role("button", name="Create", exact=True)


def duplicate_dialog_cancel(page: Page) -> Locator:
    return duplicate_dialog(page).get_by_role("button", name="Cancel", exact=True)


# ---- record detail ------------------------------------------------------------------
def detail_email_values(page: Page) -> Locator:
    return field_cell(page, "emailAddress").locator("[data-email-address]")


def detail_phone_values(page: Page) -> Locator:
    return field_cell(page, "phoneNumber").locator("[data-phone-number]")


def tasks_panel(page: Page) -> Locator:
    return page.locator('.panel[data-name="tasks"]')


def tasks_panel_create(page: Page) -> Locator:
    return tasks_panel(page).get_by_title("Create Task")


def tasks_panel_links(page: Page) -> Locator:
    """Links to the tasks listed in the contact's Tasks panel (filter: All)."""
    return tasks_panel(page).locator('.list-row [data-name="name"] a[href^="#Task/view/"]')


def tasks_panel_loaded(page: Page) -> Locator:
    """Either the panel's empty marker or its first task row."""
    return tasks_panel(page).locator(".list-container .no-data, .list-container .list-row[data-id]")
