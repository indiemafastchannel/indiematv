# IndieMA TV

A Flask-based app to stitch multiple HLS m3u8 streams into a single looping live channel.

## Setup
1. Create a repo on GitHub: https://github.com/indiemafastchannel/indiematv
2. Add the files: app.py, Dockerfile, and this README.md.
3. Deploy to Bunny CDN Magic Containers by importing the GitHub repo.

## Usage
- Access the app URL provided by Bunny CDN.
- Enter HLS m3u8 URLs in the form.
- Click "Start Channel" to begin the stream.
- The embedded player will autoplay the live channel.
- New visitors see the current live position; it loops forever until "Stop Channel" is clicked.
