from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_vibe_scheduled_research_runs_without_an_interactive_user_session():
    source = (ROOT / "hotfix" / "scripts" / "setup-vibe-research.ps1").read_text(encoding="utf-8")

    assert "/it" not in source.lower()
    assert "MT5-VibeBaseline' /tr $BaselineAction /sc daily /st 04:00 /ru SYSTEM /f" in source
    assert "MT5-VibeResearch' /tr $ResearchAction /sc weekly /d SUN /st 15:30 /ru SYSTEM /f" in source
    assert "-TimeoutMinutes 60 -SkipAgent" in source


def test_vibe_shadow_task_runs_as_system_in_the_background():
    source = (ROOT / "hotfix" / "scripts" / "register-vibe-shadow.ps1").read_text(encoding="utf-8")

    assert "/RU SYSTEM /F" in source
    assert "/it" not in source.lower()


def test_updater_preserves_background_vibe_tasks_after_a_vps_update():
    source = (ROOT / "update.ps1").read_text(encoding="utf-8")

    assert "MT5-VibeBaseline' /tr $vibeBaselineAction /sc daily /st 04:00 /ru SYSTEM /f" in source
    assert "MT5-VibeResearch' /tr $vibeAction /sc weekly /d SUN /st 15:30 /ru SYSTEM /f" in source
    assert "MT5-VibeShadow' /tr $vibeShadowAction /sc minute /mo 5 /ru SYSTEM /f" in source
    assert "-TimeoutMinutes 60 -SkipAgent" in source


def test_maintenance_and_auto_deploy_do_not_require_an_open_vnc_session():
    source = (ROOT / "update.ps1").read_text(encoding="utf-8")

    assert "MT5-Maintenance' /tr $maintenanceAction /sc daily /st 03:00 /ru SYSTEM /f" in source
    assert "MT5-AutoDeploy' /tr $adAction /sc minute /mo 15 /ru SYSTEM /rl HIGHEST /f" in source
    assert "Set-MT5TaskReliability -TaskName 'MT5-AutoDeploy' -ExecutionMinutes 45" in source
