from django.urls import path
from . import views
from .views import CompanyProjectListView, ProjectDetailView, ProjectTimelineView

urlpatterns = [
    path(
        "company/<uuid:pk>/projects/",
        CompanyProjectListView.as_view(),
        name="company_projects",
    ),
    path(
        "project/<uuid:pk>/",
        ProjectDetailView.as_view(),
        name="project_detail",
    ),
    path(
        "project/<uuid:pk>/timeline/",
        ProjectTimelineView.as_view(),
        name="project_timeline",
    ),
    path('project/add/', views.ProjectCreateView.as_view(), name='project_add'),
]
