import torch
import torch.nn as nn
import numpy as np

# =============================================================================
# PyTorch Multivariate LSTM Model Suite for Predictive MPLS NOC Copilot
#
# This file contains the complete PyTorch architecture for:
#   1. LSTMAutoencoder (Unsupervised Anomaly Detection via Reconstruction Loss)
#   2. LSTMAttentionClassifier (Supervised Prediction of Specific Failure Modes)
#   3. TimeToFailureRegressor (Regression model predicting seconds to breach)
# =============================================================================

class LSTMAutoencoder(nn.Module):
    """
    LSTM Autoencoder for Unsupervised Network Anomaly Detection.
    Learns standard network behavior; anomaly score is the reconstruction loss.
    """
    def __init__(self, sequence_length, num_features, hidden_dim=64):
        super(LSTMAutoencoder, self).__init__()
        self.sequence_length = sequence_length
        self.num_features = num_features
        self.hidden_dim = hidden_dim

        # Encoder LSTM
        self.encoder = nn.LSTM(
            input_size=num_features,
            hidden_size=hidden_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2 if hidden_dim > 16 else 0.0
        )
        
        # Decoder LSTM
        self.decoder = nn.LSTM(
            input_size=hidden_dim,
            hidden_size=hidden_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2 if hidden_dim > 16 else 0.0
        )
        
        # Output Reconstruction Layer
        self.output_layer = nn.Linear(hidden_dim, num_features)

    def forward(self, x):
        # Input shape: [Batch, SeqLen, Features]
        _, (hidden, _) = self.encoder(x)
        
        # hidden[-1] is the latent vector representation: [Batch, HiddenDim]
        latent = hidden[-1]
        
        # Repeat latent vector across sequence length to reconstruct
        # shape: [Batch, SeqLen, HiddenDim]
        decoder_input = latent.unsqueeze(1).repeat(1, self.sequence_length, 1)
        
        decoder_out, _ = self.decoder(decoder_input)
        
        # Reconstruct features at each timestep
        reconstructed = self.output_layer(decoder_out)
        return reconstructed


class Attention(nn.Module):
    """
    Attention mechanism to weigh the importance of different timesteps
    in the network history.
    """
    def __init__(self, hidden_dim):
        super(Attention, self).__init__()
        self.attention_weights = nn.Parameter(torch.randn(hidden_dim, 1))

    def forward(self, lstm_outputs):
        # lstm_outputs shape: [Batch, SeqLen, HiddenDim]
        scores = torch.matmul(lstm_outputs, self.attention_weights) # [Batch, SeqLen, 1]
        attn_weights = torch.softmax(scores, dim=1) # [Batch, SeqLen, 1]
        
        context_vector = torch.sum(lstm_outputs * attn_weights, dim=1) # [Batch, HiddenDim]
        return context_vector, attn_weights


class LSTMAttentionClassifier(nn.Module):
    """
    Supervised Sequence Classifier with Attention + Feature Heatmap.
    Predicts the exact type of network failure (e.g., OSPF flap, congestion).

    Returns (logits, attn_weights, feature_weights):
      - logits         [B, NumClasses]  — classification scores
      - attn_weights   [B, SeqLen, 1]  — per-timestep importance (existing)
      - feature_weights [B, Features]  — per-feature importance (v4 heatmap)

    feature_weights are READ-ONLY: they never feed the EPE or graph model.
    They are exposed via ACP top_features for operator-facing explainability.
    """
    def __init__(self, num_features, hidden_dim, num_classes):
        super(LSTMAttentionClassifier, self).__init__()
        self.num_features = num_features
        self.lstm = nn.LSTM(
            input_size=num_features,
            hidden_size=hidden_dim,
            num_layers=2,
            batch_first=True,
            bidirectional=True,
            dropout=0.2
        )
        self.attention = Attention(hidden_dim * 2)  # *2 for bidirectionality

        # Feature heatmap head: projects context vector → per-feature importance
        # Lightweight (hidden_dim*2 → num_features) — read-only, never in decision path
        self.feature_heatmap = nn.Linear(hidden_dim * 2, num_features)

        self.classifier = nn.Sequential(
            nn.Linear(hidden_dim * 2, 64),
            nn.ReLU(),
            nn.Dropout(0.3),
            nn.Linear(64, num_classes)
        )

    def forward(self, x):
        # x shape: [Batch, SeqLen, Features]
        lstm_out, _ = self.lstm(x)                       # [B, SeqLen, HiddenDim*2]
        context, attn_weights = self.attention(lstm_out)  # [B, HiddenDim*2]
        logits = self.classifier(context)                 # [B, NumClasses]
        feature_weights = torch.softmax(
            self.feature_heatmap(context), dim=-1         # [B, Features]
        )
        return logits, attn_weights, feature_weights


class TimeToFailureRegressor(nn.Module):
    """
    Predicts the time (seconds) remaining before a threshold breach occurs.
    """
    def __init__(self, num_features, hidden_dim=64):
        super(TimeToFailureRegressor, self).__init__()
        self.lstm = nn.LSTM(
            input_size=num_features,
            hidden_size=hidden_dim,
            num_layers=2,
            batch_first=True,
            dropout=0.2
        )
        self.fc = nn.Sequential(
            nn.Linear(hidden_dim, 32),
            nn.ReLU(),
            nn.Linear(32, 1) # Outputs a single scalar representing seconds
        )

    def forward(self, x):
        # x shape: [Batch, SeqLen, Features]
        _, (hidden, _) = self.lstm(x)
        out = self.fc(hidden[-1]) # [Batch, 1]
        return out


# =============================================================================
# Helper Functions for Data Preparation & Inference
# =============================================================================

def create_sequences(data, labels, seq_length):
    """
    Transforms flat time-series data array into sliding window sequence tensors.
    """
    xs, ys = [], []
    for i in range(len(data) - seq_length):
        x = data[i : (i + seq_length)]
        y = labels[i + seq_length]
        xs.append(x)
        ys.append(y)
    return np.array(xs), np.array(ys)


def test_models_setup():
    """
    Quick self-test to verify model tensor shapes are correct.
    """
    batch_size = 8
    seq_len = 30
    num_features = 12 # E.g., latency, jitter, loss, CPU, BW of PE1/PE2/P1
    num_classes = 6  # taxonomy.py: Healthy, latency, loss, corrupt, rate, flap
    
    # 1. Test Autoencoder
    ae = LSTMAutoencoder(seq_len, num_features, hidden_dim=32)
    inputs = torch.randn(batch_size, seq_len, num_features)
    ae_reconstructed = ae(inputs)
    print(f"Autoencoder test: Input={inputs.shape} -> Output={ae_reconstructed.shape}")
    assert ae_reconstructed.shape == inputs.shape
    
    # 2. Test Attention Classifier
    clf = LSTMAttentionClassifier(num_features, hidden_dim=32, num_classes=num_classes)
    logits, attn = clf(inputs)
    print(f"Classifier test: Output logits={logits.shape}, Attention weights={attn.shape}")
    assert logits.shape == (batch_size, num_classes)
    assert attn.shape == (batch_size, seq_len, 1)
    
    # 3. Test Regressor
    reg = TimeToFailureRegressor(num_features, hidden_dim=32)
    time_pred = reg(inputs)
    print(f"Regressor test: Output predictive TTF={time_pred.shape}")
    assert time_pred.shape == (batch_size, 1)
    
    print("[+] All model tensor sanity checks passed successfully!")

if __name__ == "__main__":
    test_models_setup()
