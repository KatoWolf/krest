"""On-demand UIA access in an isolated helper, with exact unique selectors."""

import os
from learned_model import same_app, validate_step


def _desktop():
    if os.name != "nt":
        raise ValueError("Обучение кнопкам доступно в Windows")
    from pywinauto import Desktop

    return Desktop(backend="uia")


def _identity(control):
    info = control.element_info
    return dict(
        name=str(info.name or "")[:1024],
        auto_id=str(info.automation_id or "")[:1024],
        control_type=str(info.control_type or ""),
        class_name=str(info.class_name or ""),
    )


def _parents(control, top):
    result = []
    for _ in range(2):
        control = control.parent()
        if control is None or control == top:
            break
        info = _identity(control)
        result.append({k: info[k] for k in ("auto_id", "control_type", "name")})
    return result


def _pattern(control, name):
    try:
        return getattr(control, "iface_" + name)
    except Exception:
        return None


def _matches(control, step, top):
    info = _identity(control)
    for key in ("control_type", "class_name"):
        if step[key] and info[key] != step[key]:
            return False
    key = "auto_id" if step["auto_id"] else "name"
    if info[key] != step[key]:
        return False
    actual = _parents(control, top)
    expected = step.get("parents", [])
    if len(actual) != len(expected):
        return False
    for got, wanted in zip(actual, expected):
        if got["control_type"] != wanted["control_type"]:
            return False
        key = "auto_id" if wanted["auto_id"] else "name"
        if got[key] != wanted[key]:
            return False
    return True


def _candidates(top, step):
    from pywinauto.uia_defines import IUIA
    from pywinauto.uia_element_info import UIAElementInfo
    from pywinauto.controls.uiawrapper import UIAWrapper

    uia = IUIA()
    condition = uia.build_condition(
        control_type=step["control_type"],
        class_name=step["class_name"] or None,
        title=step["name"] if not step["auto_id"] else None,
    )
    if step["auto_id"]:
        condition = uia.iuia.CreateAndCondition(
            condition,
            uia.iuia.CreatePropertyCondition(
                uia.UIA_dll.UIA_AutomationIdPropertyId, step["auto_id"]
            ),
        )
    elements = top.element_info.element.FindAll(uia.tree_scope["descendants"], condition)
    if elements.Length > 150:
        raise ValueError("Слишком много одинаковых элементов; покажите более точную кнопку")
    return [UIAWrapper(UIAElementInfo(elements.GetElement(i))) for i in range(elements.Length)]


def _find(top, step):
    controls = _candidates(top, step)
    found = [c for c in controls if c.is_visible() and c.is_enabled() and _matches(c, step, top)]
    if len(found) > 1:
        raise ValueError("Найдено несколько одинаковых элементов. Действие остановлено")
    return found[0] if found else None


def _exe(window):
    import psutil

    return psutil.Process(window.process_id()).exe()


def capture(data):
    import win32gui
    from media_rating import _enable_accessibility

    desktop = _desktop()
    foreground = win32gui.GetForegroundWindow()
    if win32gui.GetClassName(foreground).startswith("Chrome_"):
        _enable_accessibility(foreground)
    point = data.get("point") or win32gui.GetCursorPos()
    if (
        not isinstance(point, (tuple, list))
        or len(point) != 2
        or any(type(v) is not int for v in point)
    ):
        raise ValueError("Некорректная точка выбора")
    control = desktop.from_point(*point)
    top = control.top_level_parent()
    if top.process_id() in data.get("exclude_pids", []):
        raise ValueError("Наведите указатель на кнопку другой программы")
    if top.window_text() in data.get("exclude_titles", []):
        raise ValueError("Откройте другую программу: меню обучения не записывается")
    if top.handle != win32gui.GetForegroundWindow():
        raise ValueError("Сначала откройте нужное окно на переднем плане")
    # Labels/icons inside a button may be returned by ElementFromPoint.
    for _ in range(4):
        if control == top or control.element_info.element.CurrentIsPassword:
            raise ValueError("Выберите доступную кнопку, вкладку или переключатель")
        toggle = _pattern(control, "toggle")
        select = _pattern(control, "selection_item")
        expand = _pattern(control, "expand_collapse")
        invoke = _pattern(control, "invoke")
        if toggle is not None:
            current = int(toggle.CurrentToggleState)
            if current not in (0, 1):
                raise ValueError("Неоднозначное состояние переключателя")
            operation, value = "toggle", 1 - current
        elif select is not None:
            operation, value = "select", None
        elif expand is not None and int(expand.CurrentExpandCollapseState) in (0, 1):
            operation, value = (
                ("expand" if int(expand.CurrentExpandCollapseState) == 0 else "collapse"),
                None,
            )
        elif invoke is not None:
            operation, value = "invoke", None
        else:
            control = control.parent()
            continue
        step = dict(
            _identity(control),
            exe=_exe(top),
            window_class=top.class_name(),
            window_title=top.window_text()[:1024],
            parents=_parents(control, top),
            operation=operation,
            value=value,
        )
        validate_step(step)
        if _find(top, step) is None:
            raise ValueError("Не удалось однозначно найти выбранный элемент")
        return {"step": step}
    raise ValueError("Программа не предоставляет действие этой кнопки через Windows")


def perform(data):
    import win32gui

    step = validate_step(data["step"])
    top = _desktop().window(handle=win32gui.GetForegroundWindow()).wrapper_object()
    if not same_app(_exe(top), step["exe"]):
        raise ValueError(
            "Активная программа изменилась. Откройте нужную программу и запустите действие заново"
        )
    if data.get("expected_window") and top.handle != data["expected_window"]:
        raise ValueError("Активное окно изменилось. Оставшиеся шаги остановлены")
    if top.class_name() != step["window_class"] or top.window_text() != step["window_title"]:
        return {"found": False}
    control = _find(top, step)
    if control is None:
        return {"found": False}
    if data.get("probe"):
        return {"found": True, "window": top.handle}
    if control.element_info.element.CurrentIsPassword:
        raise ValueError("Доступ к защищённому полю остановлен")
    operation = step["operation"]
    pattern = _pattern(
        control,
        {
            "select": "selection_item",
            "toggle": "toggle",
            "expand": "expand_collapse",
            "collapse": "expand_collapse",
            "invoke": "invoke",
        }[operation],
    )
    if pattern is None:
        raise ValueError("Возможности кнопки изменились. Обучите шаг заново")
    # Re-check focus immediately before the one and only native invocation.
    if top.handle != win32gui.GetForegroundWindow():
        raise ValueError("Фокус изменился; действие не отправлено")
    if operation == "toggle":
        if int(pattern.CurrentToggleState) != step["value"]:
            pattern.Toggle()
        confirmed = int(pattern.CurrentToggleState) == step["value"]
    elif operation == "select":
        if not pattern.CurrentIsSelected:
            pattern.Select()
        confirmed = bool(pattern.CurrentIsSelected)
    elif operation in {"expand", "collapse"}:
        expected = 1 if operation == "expand" else 0
        if int(pattern.CurrentExpandCollapseState) != expected:
            pattern.Expand() if expected else pattern.Collapse()
        confirmed = int(pattern.CurrentExpandCollapseState) == expected
    else:
        pattern.Invoke()
        return {
            "found": True,
            "window": top.handle,
            "status": "reported",
            "text": "Приложению отправлено нажатие",
        }
    return {
        "found": True,
        "window": top.handle,
        "status": "confirmed" if confirmed else "unconfirmed",
        "text": "Состояние элемента подтверждено"
        if confirmed
        else "Элемент пока не подтвердил состояние",
    }


def locate(data, *, activate=False):
    """Select one already-open application window, never start an executable."""
    import win32gui

    step = validate_step(data["step"])
    desktop = _desktop()
    foreground = win32gui.GetForegroundWindow()
    candidates = []
    for window in desktop.windows(class_name=step["window_class"], visible_only=False):
        try:
            if not same_app(_exe(window), step["exe"]):
                continue
            if window.window_text() != step["window_title"]:
                continue
            if window.handle == foreground:
                candidates = [window]
                break
            if window.window_text() == step["window_title"]:
                candidates.append(window)
        except Exception:
            continue
    if len(candidates) != 1:
        raise ValueError("Откройте исходное окно программы: нужен один однозначный вариант")
    top = candidates[0]
    if top.class_name().startswith("Chrome_"):
        from media_rating import _enable_accessibility

        _enable_accessibility(top.handle)
    if activate:
        top.set_focus()
        if top.handle != win32gui.GetForegroundWindow():
            raise ValueError("Windows не переключила окно. Откройте его вручную")
    return {"found": _find(top, step) is not None, "window": top.handle}


def dispatch(action, data):
    if action == "capture":
        return capture(data)
    if action == "perform":
        return perform(data)
    if action in {"locate", "activate"}:
        return locate(data, activate=action == "activate")
    raise ValueError("Неизвестное действие обучения")
