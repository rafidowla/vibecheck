import time
from pynput import mouse

print("Starting lightweight pynput listener test.")
print("Click anywhere. Press Ctrl+C to stop.")

def on_click(x, y, button, pressed):
    if pressed:
        print(f"[{time.time():.3f}] CLICK: ({x:.1f}, {y:.1f}) btn={button.name}")

try:
    with mouse.Listener(on_click=on_click) as listener:
        listener.join()
except KeyboardInterrupt:
    print("\nStopped.")
