package com.datasell.app;

import android.content.Context;
import android.util.Log;
import androidx.annotation.NonNull;
import androidx.work.Worker;
import androidx.work.WorkerParameters;

/** Runs the daily eBay update in the background when it's due, then saves the backup. */
public class UpdateWorker extends Worker {
    public UpdateWorker(@NonNull Context context, @NonNull WorkerParameters params) {
        super(context, params);
    }

    @NonNull
    @Override
    public Result doWork() {
        Context context = getApplicationContext();
        try {
            DatasellApp.ensurePython(context);
            boolean ran = DatasellApp.module()
                    .callAttr("run_if_due", DatasellApp.filesDir(context)).toBoolean();
            if (ran) {
                BackupExporter.export(context);
            }
        } catch (Throwable t) {
            Log.e("Datasell", "Daily update failed", t);
        }
        // Always "success": the next hourly check tries again if needed.
        return Result.success();
    }
}
