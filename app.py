    AI_PENDING_TASKS.pop(token, None)
    return data.get("payload")

def _build_openai_messages(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    prepared: List[Dict[str, Any]] = []
    for msg in messages:
        role = (msg.get("role") or "").strip().lower()
        if role not in ("user", "assistant"):
            continue
        text = (msg.get("text") or "").strip()
        attachments = msg.get("attachments") or []
        content: List[Dict[str, Any]] = []
        # Responses API input messages use input_text for user/assistant context.
        if text:
            content.append({"type": "input_text", "text": text})
        for att in attachments:
            atype = (att.get("type") or "").strip().lower()
            if atype != "image":
                continue
            data = (att.get("data") or "").strip()
            mime = (att.get("mime_type") or "image/png").strip() or "image/png"
            if not data:
                continue
            # Validate base64 to avoid invalid payloads
            try:
                base64.b64decode(data, validate=True)
            except Exception:
                continue
            # The Responses API variant in some deployments expects an image URL
            # rather than a bespoke 'image_base64' field. Use a data: URL which
            # is widely supported as an image_url fallback.
            # 'input_image' is commonly accepted, but if the client rejects it
            # we still provide the data URL which many assistants can consume.
            content.append({"type": "input_image", "image_url": f"data:{mime};base64,{data}"})
        if not content and not text:
            continue
        # attach the prepared content for this message
        prepared.append({"role": role, "content": content or [{"type": "input_text", "text": text or ""}]})
    return prepared


PROTECTED_PROJECT_PATHS = {".env", ".env.local", ".env.production", "data"}
PROTECTED_PROJECT_DIRS = {".git", ".hg", ".svn"}


def _resolve_project_path(rel_path: str) -> Path:
    rel_path = (rel_path or "").strip().replace("\\", "/")
    if not rel_path:
        raise ValueError("مسیر فایل مشخص نشده است.")
    rel = Path(rel_path)
    if rel.is_absolute() or ".." in rel.parts:
        raise ValueError("مسیر باید نسبی و داخل پروژه باشد.")
    if rel.parts and (rel.parts[0] in PROTECTED_PROJECT_DIRS or rel.parts[0] in PROTECTED_PROJECT_PATHS):
        raise ValueError("این مسیر برای ویرایش توسط دستیار محافظت شده است.")
    target = (PROJECT_ROOT / rel).resolve()
    if PROJECT_ROOT not in target.parents and target != PROJECT_ROOT:
        raise ValueError("امکان دسترسی به مسیر خارج از پروژه وجود ندارد.")
    return target


def _apply_assistant_actions(actions: List[Dict[str, Any]]) -> Dict[str, Any]:
    if not is_admin():
        return {
            "applied": 0,
            "failed": len(actions or []),
            "messages": [],
            "errors": ["اعمال تغییرات فایل پروژه فقط برای مدیر سیستم مجاز است."],
        }
    summary = {"applied": 0, "failed": 0, "messages": [], "errors": []}
    for action in actions:
        if not isinstance(action, dict):
            summary["failed"] += 1
            summary["errors"].append("ساختار عملیات نامعتبر است.")
            continue
        operation = (action.get("operation") or action.get("type") or "").strip().lower()
        rel_path = (action.get("path") or "").strip()
        description = (action.get("description") or "").strip()
        label = description or rel_path or "عملیات فایل"
        if not operation:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: نوع عملیات مشخص نشده است.")
            continue
        if not rel_path:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: مسیر فایل مشخص نیست.")
            continue
        try:
            target = _resolve_project_path(rel_path)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: {exc}")
            continue

        try:
            if operation == "write_file":
                content = action.get("content")
                if not isinstance(content, str):
                    raise ValueError("محتوای فایل موجود نیست.")
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(content, encoding="utf-8")
                summary["applied"] += 1
                message = description or f"فایل «{rel_path}» بروزرسانی شد."
            elif operation == "append_file":
                content = action.get("content")
                if not isinstance(content, str):
                    raise ValueError("محتوای فایل موجود نیست.")
                target.parent.mkdir(parents=True, exist_ok=True)
                with target.open("a", encoding="utf-8") as fh:
                    fh.write(content)
                summary["applied"] += 1
                message = description or f"محتوا به فایل «{rel_path}» افزوده شد."
            elif operation == "delete_path":
                if target.is_dir():
                    raise ValueError("حذف پوشه مجاز نیست.")
                if target.exists():
                    target.unlink()
                summary["applied"] += 1
                message = description or f"فایل «{rel_path}» حذف شد."
            else:
                raise ValueError(f"عملیات ناشناخته: {operation}")

            summary["messages"].append(message)
            app.logger.info("AI_ACTION %s %s", operation.upper(), rel_path)
        except Exception as exc:
            summary["failed"] += 1
            summary["errors"].append(f"{label}: {exc}")
            app.logger.exception("AI_ACTION_FAILED operation=%s path=%s", operation or "?", rel_path)
    return summary


def _call_openai_assistant(messages: List[Dict[str, Any]]) -> Dict[str, Any]:
    # دریافت تنظیمات شخصی کاربر
    username = getattr(current_user, "username", "admin")
    user_settings = UserSettings.get_for_user(username)
    
    # استفاده از کلید API شخصی یا سراسری
    api_key = user_settings.openai_api_key if user_settings.openai_api_key else _openai_api_key()
    if not api_key:
        raise RuntimeError("کلید API تنظیم نشده است.")
    if OpenAI is None:
        raise RuntimeError("کتابخانه openai نصب نشده است.")

    # استفاده از مدل شخصی یا پیش‌فرض
    model = user_settings.openai_model if user_settings.openai_model else _assistant_model()
    
    # استفاده از دستورالعمل سیستمی شخصی یا پیش‌فرض
    if user_settings.system_prompt:
        system_prompt = user_settings.system_prompt
    else:
        system_prompt = (
            "شما دستیار هوشمند حساب‌پاک هستید. وظیفه شما استخراج اطلاعات فاکتورهای خرید و فروش"
            " از متن یا تصویر و هدایت کاربر برای ثبت آن‌ها در سیستم است. همواره پاسخ نهایی را"
            " به زبان فارسی بدهید و در صورت ابهام، مواردی که نیاز به تأیید دارند را مشخص کنید."
            " اگر تصویر فاکتور دریافت کردید مقادیر تاریخ، شماره فاکتور، نام خریدار/فروشنده و"
            " اقلام را با قیمت و تعداد استخراج کنید. در صورت نبود قیمت، مقدار null قرار دهید."
            " در صورتی که کاربر درخواست ویرایش رابط کاربری یا کد برنامه را داشت، ابتدا راهکار"
            " را توضیح دهید و سپس در فیلد actions فهرستی از عملیات فایل را ارائه کنید. برای"
            " عملیات write_file محتوای کامل فایل هدف را ارسال کنید و مسیرها را نسبت به ریشه"
            " پروژه بنویسید."
        )

    client = OpenAI(api_key=api_key)

    request_messages = [
        {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]}
    ] + _build_openai_messages(messages)

    # استفاده از temperature شخصی
    temperature = user_settings.temperature if user_settings.temperature is not None else 0.7