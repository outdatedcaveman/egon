"""Page widgets. One file per nav item (or shared via generic.make_page)."""
from egon_app.pages.home import HomePage
from egon_app.pages.inbox import InboxPage
from egon_app.pages.navigation import NavigationPage
from egon_app.pages.ledger import LedgerPage
from egon_app.pages.sync import SyncPage
from egon_app.pages.memory import MemoryPage
from egon_app.pages.settings import SettingsPage
from egon_app.pages.references import ReferencesPage
from egon_app.pages.media import MediaPage
from egon_app.pages.search import SearchPage
from egon_app.pages.mind import MindPage
from egon_app.pages.projects import ProjectsPage
from egon_app.pages.connect import ConnectPage
from egon_app.pages.generic import make_page as make_generic_page

__all__ = [
    "HomePage", "InboxPage", "NavigationPage", "LedgerPage", "SyncPage",
    "MemoryPage", "SettingsPage", "ReferencesPage", "MediaPage",
    "SearchPage", "MindPage", "ProjectsPage", "ConnectPage", "make_generic_page",
]
