from park_service import create_app
import os

app = create_app()

if __name__ == "__main__":
    dispatcher = app.extensions['feishu_dispatcher']
    try:
        dispatcher.start()
        app.run(host="127.0.0.1", port=int(os.environ.get('PARK_PORT','5000')), debug=False)
    finally:
        dispatcher.close()
