import cv2
pipe = ("udpsrc port=5000 caps=\"application/x-rtp,media=video,encoding-name=H264,payload=96\" "
        "! rtph264depay ! h264parse ! avdec_h264 max-threads=1 "
        "! videoconvert ! video/x-raw,format=BGR "
        "! appsink drop=true max-buffers=1 sync=false")
cap = cv2.VideoCapture(pipe, cv2.CAP_GSTREAMER)
