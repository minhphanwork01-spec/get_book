# Streamlit Web Text Extractor

## Local run

```bash
python -m pip install -r requirements_streamlit_web_text_extractor.txt
streamlit run streamlit_web_text_extractor_app.py
```

## Deploy on Streamlit Cloud

Put these files in a GitHub repo:

- `streamlit_web_text_extractor_app.py`
- `web_text_extractor_v3_core.py`
- `requirements_streamlit_web_text_extractor.txt`

Main file path:

```text
streamlit_web_text_extractor_app.py
```

## Recommended workflow

1. Set `Max chapters = 5`.
2. Run test.
3. Check TXT/EPUB output.
4. Set `Max chapters = 0` for full crawl.

## Notes

- `#list-chapter` is a browser fragment. The app removes it before request.
- If a site blocks cloud IP, try local Streamlit with `Verify SSL certificate` off, or upload local HTML.
- For full 2,000+ chapters, Streamlit Cloud may be unstable. Local Streamlit or a VPS is more reliable.
