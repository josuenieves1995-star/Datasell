package com.datasell.app;

import android.content.ContentResolver;
import android.content.ContentValues;
import android.content.Context;
import android.database.Cursor;
import android.net.Uri;
import android.os.Environment;
import android.provider.MediaStore;
import android.util.Log;
import java.io.File;
import java.io.FileInputStream;
import java.io.InputStream;
import java.io.OutputStream;

/**
 * Copies the backup Python wrote (files/backup/) into the phone's
 * Documents/Datasell folder, where it shows up in the Files app and survives
 * uninstalling the app.
 */
final class BackupExporter {
    static final String FOLDER = Environment.DIRECTORY_DOCUMENTS + "/Datasell";

    private BackupExporter() {}

    /** Returns true if the files were copied. */
    static synchronized boolean export(Context context) {
        File dir = new File(context.getFilesDir(), "backup");
        boolean ok = copy(context, new File(dir, "datasell.db"), "application/octet-stream");
        ok &= copy(context, new File(dir, "datasell_solds.csv"), "text/csv");
        return ok;
    }

    private static boolean copy(Context context, File source, String mime) {
        if (!source.exists()) {
            return false;
        }
        ContentResolver resolver = context.getContentResolver();
        Uri collection = MediaStore.Files.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY);
        Uri target = null;
        // Overwrite our earlier copy if there is one (we can only see files this app created).
        try (Cursor c = resolver.query(collection, new String[] {MediaStore.MediaColumns._ID},
                MediaStore.MediaColumns.RELATIVE_PATH + "=? AND " + MediaStore.MediaColumns.DISPLAY_NAME + "=?",
                new String[] {FOLDER + "/", source.getName()}, null)) {
            if (c != null && c.moveToFirst()) {
                target = Uri.withAppendedPath(collection, String.valueOf(c.getLong(0)));
            }
        } catch (Exception e) {
            Log.w("Datasell", "Backup lookup failed", e);
        }
        try {
            if (target == null) {
                ContentValues values = new ContentValues();
                values.put(MediaStore.MediaColumns.DISPLAY_NAME, source.getName());
                values.put(MediaStore.MediaColumns.MIME_TYPE, mime);
                values.put(MediaStore.MediaColumns.RELATIVE_PATH, FOLDER);
                target = resolver.insert(collection, values);
            }
            if (target == null) {
                return false;
            }
            try (InputStream in = new FileInputStream(source);
                 OutputStream out = resolver.openOutputStream(target, "wt")) {
                byte[] buf = new byte[64 * 1024];
                int n;
                while ((n = in.read(buf)) > 0) {
                    out.write(buf, 0, n);
                }
            }
            return true;
        } catch (Exception e) {
            Log.e("Datasell", "Backup copy failed", e);
            return false;
        }
    }
}
