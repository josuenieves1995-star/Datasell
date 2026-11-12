package com.datasell.app;

import com.anthropic.client.AnthropicClient;
import com.anthropic.client.okhttp.AnthropicOkHttpClient;
import com.anthropic.core.JsonValue;
import com.anthropic.errors.AnthropicIoException;
import com.anthropic.errors.AnthropicServiceException;
import com.anthropic.errors.BadRequestException;
import com.anthropic.errors.PermissionDeniedException;
import com.anthropic.errors.RateLimitException;
import com.anthropic.errors.UnauthorizedException;
import com.anthropic.models.beta.AnthropicBeta;
import com.anthropic.models.beta.messages.BetaBase64ImageSource;
import com.anthropic.models.beta.messages.BetaContentBlock;
import com.anthropic.models.beta.messages.BetaContentBlockParam;
import com.anthropic.models.beta.messages.BetaImageBlockParam;
import com.anthropic.models.beta.messages.BetaJsonOutputFormat;
import com.anthropic.models.beta.messages.BetaMessage;
import com.anthropic.models.beta.messages.BetaOutputConfig;
import com.anthropic.models.beta.messages.BetaStopReason;
import com.anthropic.models.beta.messages.MessageCreateParams;
import com.fasterxml.jackson.core.type.TypeReference;
import com.fasterxml.jackson.databind.ObjectMapper;
import java.time.Duration;
import java.util.ArrayList;
import java.util.List;
import java.util.Map;

/**
 * Calls Claude to identify a clothing item from photos. Used by the Android app
 * because the Python SDK can't be installed there; the Python side
 * (datasell/android.py) builds the prompt and parses the answer.
 */
public final class ClaudeBridge {
    private ClaudeBridge() {}

    /** Thrown with a message that can be shown to the user as-is. */
    public static final class ClaudeError extends Exception {
        public ClaudeError(String message, Throwable cause) { super(message, cause); }
    }

    /**
     * @param frontJpegB64 base64 JPEG of the front of the item
     * @param tagJpegB64   base64 JPEG of the tag, or null/empty
     * @param baseUrl      null for the real API (tests point it at a local server)
     * @return the JSON text Claude returned (matching schemaJson)
     */
    public static String identify(String apiKey, String model, String system, String schemaJson,
                                  String frontJpegB64, String tagJpegB64, String promptText,
                                  String baseUrl) throws ClaudeError {
        Map<String, Object> schemaMap;
        try {
            schemaMap = new ObjectMapper().readValue(schemaJson, new TypeReference<Map<String, Object>>() {});
        } catch (Exception e) {
            throw new ClaudeError("Internal error: bad schema.", e);
        }
        BetaJsonOutputFormat.Schema.Builder schema = BetaJsonOutputFormat.Schema.builder();
        for (Map.Entry<String, Object> entry : schemaMap.entrySet()) {
            schema.putAdditionalProperty(entry.getKey(), JsonValue.from(entry.getValue()));
        }

        List<BetaContentBlockParam> content = new ArrayList<>();
        content.add(BetaContentBlockParam.ofText("Front of the item:"));
        content.add(image(frontJpegB64));
        if (tagJpegB64 != null && !tagJpegB64.isEmpty()) {
            content.add(BetaContentBlockParam.ofText("Tag:"));
            content.add(image(tagJpegB64));
        }
        content.add(BetaContentBlockParam.ofText(promptText));

        MessageCreateParams params = MessageCreateParams.builder()
                .model(model)
                .maxTokens(16000)
                .system(system)
                .addUserMessageOfContentBlockParams(content)
                .outputConfig(BetaOutputConfig.builder()
                        .effort(BetaOutputConfig.Effort.MEDIUM)
                        .format(BetaJsonOutputFormat.builder().schema(schema.build()).build())
                        .build())
                // If the model declines, the API retries on a fallback model.
                .fallbacksDefault()
                .addBeta(AnthropicBeta.SERVER_SIDE_FALLBACK_2026_07_01)
                .build();

        AnthropicOkHttpClient.Builder builder = AnthropicOkHttpClient.builder()
                .apiKey(apiKey)
                .timeout(Duration.ofMinutes(3))
                .maxRetries(2);
        if (baseUrl != null && !baseUrl.isEmpty()) {
            builder.baseUrl(baseUrl);
        }
        AnthropicClient client = builder.build();

        BetaMessage message;
        try {
            message = client.beta().messages().create(params);
        } catch (UnauthorizedException | PermissionDeniedException e) {
            throw new ClaudeError("Your Claude API key is missing or invalid. Check it in Settings.", e);
        } catch (RateLimitException e) {
            throw new ClaudeError("Claude API rate limit reached. Try again in a minute.", e);
        } catch (BadRequestException e) {
            throw new ClaudeError("Claude API rejected the request: " + e.getMessage(), e);
        } catch (AnthropicServiceException e) {
            throw new ClaudeError("Claude API error (" + e.statusCode() + "). Try again shortly.", e);
        } catch (AnthropicIoException e) {
            throw new ClaudeError("Couldn't reach the Claude API. Check your internet connection.", e);
        } finally {
            client.close();
        }

        BetaStopReason stop = message.stopReason().orElse(null);
        if (BetaStopReason.REFUSAL.equals(stop)) {
            throw new ClaudeError("Claude couldn't identify this photo. Try a clearer photo.", null);
        }
        if (BetaStopReason.MAX_TOKENS.equals(stop)) {
            throw new ClaudeError("Claude's answer was cut off. Please try again.", null);
        }
        for (BetaContentBlock block : message.content()) {
            if (block.text().isPresent()) {
                return block.text().get().text();
            }
        }
        throw new ClaudeError("Claude returned an empty answer. Please try again.", null);
    }

    private static BetaContentBlockParam image(String b64) {
        return BetaContentBlockParam.ofImage(BetaImageBlockParam.builder()
                .source(BetaBase64ImageSource.builder()
                        .mediaType(BetaBase64ImageSource.MediaType.IMAGE_JPEG)
                        .data(b64)
                        .build())
                .build());
    }
}
