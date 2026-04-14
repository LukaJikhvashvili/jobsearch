from job_applier import apply


result = apply(
    url="https://hel-ai.com/apply/rG3G9Ced",
    cv_path="Luka Jikhvashvili CV.pdf",
    auto_submit=True,
    headless=False,
)
print(result)