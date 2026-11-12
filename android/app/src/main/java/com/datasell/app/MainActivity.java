package com.datasell.app;

import android.app.Activity;
import android.content.Intent;
import android.graphics.Color;
import android.net.Uri;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.provider.MediaStore;
import android.view.Gravity;
import android.webkit.JavascriptInterface;
import android.webkit.ValueCallback;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceRequest;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.TextView;
import android.widget.Toast;
import androidx.core.content.FileProvider;
import java.io.File;
import java.io.FileOutputStream;
import java.io.InputStream;
import java.io.OutputStream;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;

/** Shows the Datasell pages (served by Python on the phone) in a full-screen web view. */
public class MainActivity extends Activity {
    private static final int REQ_PHOTO = 1;
    private static final int REQ_RESTORE = 2;

    private final ExecutorService background = Executors.newSingleThreadExecutor();
    private final Handler main = new Handler(Looper.getMainLooper());
    private WebView webView;
    private ValueCallback<Uri[]> photoCallback;
    private Uri cameraUri;
    private int port;

    @Override
    protected void onCreate(Bundle savedInstanceState) {
        super.onCreate(savedInstanceState);
        TextView loading = new TextView(this);
        loading.setText("Starting Datasell…");
        loading.setTextSize(18);
        loading.setGravity(Gravity.CENTER);
        loading.setBackgroundColor(Color.parseColor("#f6f5f2"));
        loading.setTextColor(Color.parseColor("#1d1c1a"));
        setContentView(loading);

        background.execute(() -> {
            try {
                port = DatasellApp.startServer(this);
                main.post(this::showApp);
            } catch (Throwable t) {
                main.post(() -> loading.setText("Datasell couldn't start:\n\n" + t.getMessage()));
            }
        });
    }

    private void showApp() {
        webView = new WebView(this);
        WebSettings s = webView.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(true);
        webView.addJavascriptInterface(new AndroidBridge(), "DatasellAndroid");

        webView.setWebViewClient(new WebViewClient() {
            @Override
            public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
                Uri url = request.getUrl();
                if ("127.0.0.1".equals(url.getHost())) {
                    return false;
                }
                // eBay and other links open in the phone's browser / eBay app.
                try {
                    startActivity(new Intent(Intent.ACTION_VIEW, url));
                } catch (Exception ignored) {
                }
                return true;
            }
        });

        webView.setWebChromeClient(new WebChromeClient() {
            @Override
            public boolean onShowFileChooser(WebView view, ValueCallback<Uri[]> callback,
                                             FileChooserParams params) {
                if (photoCallback != null) {
                    photoCallback.onReceiveValue(null);
                }
                photoCallback = callback;
                startActivityForResult(photoChooser(), REQ_PHOTO);
                return true;
            }
        });

        setContentView(webView);
        webView.loadUrl("http://127.0.0.1:" + port + "/check");
    }

    /** Take a photo with the camera, or pick one from the gallery. */
    private Intent photoChooser() {
        Intent pick = new Intent(Intent.ACTION_GET_CONTENT);
        pick.addCategory(Intent.CATEGORY_OPENABLE);
        pick.setType("image/*");
        Intent chooser = Intent.createChooser(pick, "Add a photo");
        try {
            File dir = new File(getCacheDir(), "photos");
            dir.mkdirs();
            File photo = new File(dir, "photo_" + System.currentTimeMillis() + ".jpg");
            cameraUri = FileProvider.getUriForFile(this, getPackageName() + ".files", photo);
            Intent camera = new Intent(MediaStore.ACTION_IMAGE_CAPTURE);
            camera.putExtra(MediaStore.EXTRA_OUTPUT, cameraUri);
            camera.addFlags(Intent.FLAG_GRANT_WRITE_URI_PERMISSION | Intent.FLAG_GRANT_READ_URI_PERMISSION);
            chooser.putExtra(Intent.EXTRA_INITIAL_INTENTS, new Intent[] {camera});
        } catch (Exception e) {
            cameraUri = null;
        }
        return chooser;
    }

    @Override
    protected void onActivityResult(int requestCode, int resultCode, Intent data) {
        super.onActivityResult(requestCode, resultCode, data);
        if (requestCode == REQ_PHOTO && photoCallback != null) {
            Uri[] result = null;
            if (resultCode == RESULT_OK) {
                if (data != null && data.getData() != null) {
                    result = new Uri[] {data.getData()};
                } else if (cameraUri != null) {
                    result = new Uri[] {cameraUri};
                }
            }
            photoCallback.onReceiveValue(result);
            photoCallback = null;
        } else if (requestCode == REQ_RESTORE && resultCode == RESULT_OK && data != null && data.getData() != null) {
            restore(data.getData());
        }
    }

    private void restore(Uri uri) {
        toast("Restoring…");
        background.execute(() -> {
            String error;
            try {
                File tmp = new File(getCacheDir(), "restore.db");
                try (InputStream in = getContentResolver().openInputStream(uri);
                     OutputStream out = new FileOutputStream(tmp)) {
                    byte[] buf = new byte[64 * 1024];
                    int n;
                    while ((n = in.read(buf)) > 0) {
                        out.write(buf, 0, n);
                    }
                }
                error = DatasellApp.module().callAttr("restore_from",
                        DatasellApp.filesDir(this), tmp.getAbsolutePath()).toString();
                tmp.delete();
            } catch (Throwable t) {
                error = "Couldn't read that file.";
            }
            final String message = error.isEmpty() ? "Backup restored." : error;
            main.post(() -> {
                toast(message);
                if (webView != null) {
                    webView.loadUrl("http://127.0.0.1:" + port + "/check");
                }
            });
        });
    }

    private void toast(String message) {
        Toast.makeText(this, message, Toast.LENGTH_LONG).show();
    }

    @Override
    public void onBackPressed() {
        if (webView != null && webView.canGoBack()) {
            webView.goBack();
        } else {
            super.onBackPressed();
        }
    }

    @Override
    protected void onStop() {
        super.onStop();
        // Keep Documents/Datasell up to date with any checks made today.
        background.execute(() -> {
            try {
                DatasellApp.module().callAttr("backup_now", DatasellApp.filesDir(this));
                BackupExporter.export(getApplicationContext());
            } catch (Throwable ignored) {
            }
        });
    }

    /** Buttons on the Settings page call these. */
    private class AndroidBridge {
        @JavascriptInterface
        public void backupNow() {
            background.execute(() -> {
                boolean ok;
                try {
                    DatasellApp.module().callAttr("backup_now", DatasellApp.filesDir(MainActivity.this));
                    ok = BackupExporter.export(getApplicationContext());
                } catch (Throwable t) {
                    ok = false;
                }
                final boolean saved = ok;
                main.post(() -> toast(saved ? "Saved to Documents/Datasell" : "Backup failed. Try again."));
            });
        }

        @JavascriptInterface
        public void restoreBackup() {
            main.post(() -> {
                Intent pick = new Intent(Intent.ACTION_OPEN_DOCUMENT);
                pick.addCategory(Intent.CATEGORY_OPENABLE);
                pick.setType("*/*");
                startActivityForResult(pick, REQ_RESTORE);
            });
        }
    }
}
