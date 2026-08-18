import streamlit as st

st.title("Test OK")
st.write("Kalau ini muncul, platform Streamlit Cloud-nya sehat — masalahnya ada di app.py yang asli.")

st.write("Cek import satu-satu:")
try:
    import imageio_ffmpeg
    ff = imageio_ffmpeg.get_ffmpeg_exe()
    st.success(f"imageio_ffmpeg OK -> {ff}")
except Exception as e:
    st.error("imageio_ffmpeg GAGAL")
    st.exception(e)

try:
    import edge_tts
    st.success("edge_tts OK")
except Exception as e:
    st.error("edge_tts GAGAL")
    st.exception(e)

try:
    from PIL import Image
    st.success("Pillow OK")
except Exception as e:
    st.error("Pillow GAGAL")
    st.exception(e)
