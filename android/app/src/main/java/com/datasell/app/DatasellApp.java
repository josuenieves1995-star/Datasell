package com.datasell.app;

import android.app.Application;
import android.content.Context;
import com.chaquo.python.PyObject;
import com.chaquo.python.Python;
import com.chaquo.python.android.AndroidPlatform;
import androidx.work.Constraints;
import androidx.work.ExistingPeriodicWorkPolicy;
import androidx.work.NetworkType;
import androidx.work.PeriodicWorkRequest;
import androidx.work.WorkManager;
import java.util.concurrent.TimeUnit;

/** Starts Python once per process and schedules the nightly update. */
public class DatasellApp extends Application {
    private static int port = 0;

    @Override
    public void onCreate() {
        super.onCreate();
        ensurePython(this);
        scheduleUpdates(this);
    }

    static void ensurePython(Context context) {
        if (!Python.isStarted()) {
            Python.start(new AndroidPlatform(context.getApplicationContext()));
        }
    }

    static PyObject module() {
        return Python.getInstance().getModule("datasell.android");
    }

    static String filesDir(Context context) {
        return context.getFilesDir().getAbsolutePath();
    }

    /** Starts the local web server (first call only) and returns its port. Slow: call off the UI thread. */
    static synchronized int startServer(Context context) {
        if (port == 0) {
            ensurePython(context);
            final Context app = context.getApplicationContext();
            Runnable onBackup = () -> BackupExporter.export(app);
            port = module().callAttr("start", filesDir(app), onBackup).toInt();
        }
        return port;
    }

    /**
     * Wakes the app about once an hour (when online). Python decides whether the
     * daily update is due (after 11:30 PM, or a missed one), so it runs once a day.
     */
    static void scheduleUpdates(Context context) {
        Constraints constraints = new Constraints.Builder()
                .setRequiredNetworkType(NetworkType.CONNECTED)
                .build();
        PeriodicWorkRequest request = new PeriodicWorkRequest.Builder(UpdateWorker.class, 1, TimeUnit.HOURS)
                .setConstraints(constraints)
                .build();
        WorkManager.getInstance(context).enqueueUniquePeriodicWork(
                "datasell-daily-update", ExistingPeriodicWorkPolicy.KEEP, request);
    }
}
