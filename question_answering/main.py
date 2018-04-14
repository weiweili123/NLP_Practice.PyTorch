from loader import Dataset, pad_collate
import os
import tensorflow as tf
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.nn.init as init
from torch.autograd import Variable
from torch.utils.data import DataLoader

def add_summary_value(writer, key, value, iteration):
    summary = tf.Summary(value=[tf.Summary.Value(tag=key, simple_value=value)])
    writer.add_summary(summary, iteration)

def position_encoding(embedded_sentence):
    '''
    embedded_sentence.size() -> (#batch, #sentence, #token, #embedding)
    l.size() -> (#sentence, #embedding)
    output.size() -> (#batch, #sentence, #embedding)
    '''
    _, _, slen, elen = embedded_sentence.size()

    l = [[(1 - s/(slen-1)) - (e/(elen-1)) * (1 - 2*s/(slen-1)) for e in range(elen)] for s in range(slen)]
    l = torch.FloatTensor(l)
    l = l.unsqueeze(0) # for #batch
    l = l.unsqueeze(1) # for #sen
    l = l.expand_as(embedded_sentence)
    weighted = embedded_sentence * Variable(l.cuda())
    return torch.sum(weighted, dim=2).squeeze(2) # sum with tokens

class AttentionGRUCell(nn.Module):
    def __init__(self, input_size, hidden_size):
        super(AttentionGRUCell, self).__init__()
        self.hidden_size = hidden_size
        self.Wr = nn.Linear(input_size, hidden_size)
        init.xavier_normal(self.Wr.state_dict()['weight'])
        self.Ur = nn.Linear(hidden_size, hidden_size)
        init.xavier_normal(self.Ur.state_dict()['weight'])
        self.W = nn.Linear(input_size, hidden_size)
        init.xavier_normal(self.W.state_dict()['weight'])
        self.U = nn.Linear(hidden_size, hidden_size)
        init.xavier_normal(self.U.state_dict()['weight'])

    def forward(self, fact, C, g):
        '''
        fact.size() -> (#batch, #hidden = #embedding)
        c.size() -> (#hidden, ) -> (#batch, #hidden = #embedding)
        r.size() -> (#batch, #hidden = #embedding)
        h_tilda.size() -> (#batch, #hidden = #embedding)
        g.size() -> (#batch, )
        '''

        r = F.sigmoid(self.Wr(fact) + self.Ur(C))
        h_tilda = F.tanh(self.W(fact) + r * self.U(C))
        g = g.unsqueeze(1).expand_as(h_tilda)
        h = g * h_tilda + (1 - g) * C
        return h

class AttentionGRU(nn.Module):
    def __init__(self, input_size, hidden_size):
        super(AttentionGRU, self).__init__()
        self.hidden_size = hidden_size
        self.AGRUCell = AttentionGRUCell(input_size, hidden_size)

    def forward(self, facts, G):
        '''
        facts.size() -> (#batch, #sentence, #hidden = #embedding)
        fact.size() -> (#batch, #hidden = #embedding)
        G.size() -> (#batch, #sentence)
        g.size() -> (#batch, )
        C.size() -> (#batch, #hidden)
        '''
        batch_num, sen_num, embedding_size = facts.size()
        C = Variable(torch.zeros(self.hidden_size)).cuda()
        for sid in range(sen_num):
            fact = facts[:, sid, :]
            g = G[:, sid]
            if sid == 0:
                C = C.unsqueeze(0).expand_as(fact)
            C = self.AGRUCell(fact, C, g)
        return C

class EpisodicMemory(nn.Module):
    def __init__(self, hidden_size):
        super(EpisodicMemory, self).__init__()
        self.AGRU = AttentionGRU(hidden_size, hidden_size)
        self.z1 = nn.Linear(4 * hidden_size, hidden_size)
        self.z2 = nn.Linear(hidden_size, 1)
        self.next_mem = nn.Linear(3 * hidden_size, hidden_size)
        init.xavier_normal(self.z1.state_dict()['weight'])
        init.xavier_normal(self.z2.state_dict()['weight'])
        init.xavier_normal(self.next_mem.state_dict()['weight'])

    def make_interaction(self, facts, questions, prevM):
        '''
        facts.size() -> (#batch, #sentence, #hidden = #embedding)
        questions.size() -> (#batch, 1, #hidden)
        prevM.size() -> (#batch, #sentence = 1, #hidden = #embedding)
        z.size() -> (#batch, #sentence, 4 x #embedding)
        G.size() -> (#batch, #sentence)
        '''
        batch_num, sen_num, embedding_size = facts.size()
        questions = questions.expand_as(facts)
        prevM = prevM.expand_as(facts)

        z = torch.cat([
            facts * questions,
            facts * prevM,
            torch.abs(facts - questions),
            torch.abs(facts - prevM)
        ], dim=2)

        z = z.view(-1, 4 * embedding_size)

        G = F.tanh(self.z1(z))
        G = self.z2(G)
        G = G.view(batch_num, -1)
        G = F.softmax(G)

        return G

    def forward(self, facts, questions, prevM):
        '''
        facts.size() -> (#batch, #sentence, #hidden = #embedding)
        questions.size() -> (#batch, #sentence = 1, #hidden)
        prevM.size() -> (#batch, #sentence = 1, #hidden = #embedding)
        G.size() -> (#batch, #sentence)
        C.size() -> (#batch, #hidden)
        concat.size() -> (#batch, 3 x #embedding)
        '''
        G = self.make_interaction(facts, questions, prevM)
        C = self.AGRU(facts, G)
        concat = torch.cat([prevM.squeeze(1), C, questions.squeeze(1)], dim=1)
        next_mem = F.relu(self.next_mem(concat))
        next_mem = next_mem.unsqueeze(1)
        return next_mem


class QuestionModule(nn.Module):
    def __init__(self, vocab_size, hidden_size):
        super(QuestionModule, self).__init__()
        self.gru = nn.GRU(hidden_size, hidden_size, batch_first=True)

    def forward(self, questions, word_embedding):
        '''
        questions.size() -> (#batch, #token)
        word_embedding() -> (#batch, #token, #embedding)
        gru() -> (1, #batch, #hidden)
        '''
        questions = word_embedding(questions)
        _, questions = self.gru(questions)
        questions = questions.transpose(0, 1)
        return questions

class InputModule(nn.Module):
    def __init__(self, vocab_size, hidden_size):
        super(InputModule, self).__init__()
        self.hidden_size = hidden_size
        self.gru = nn.GRU(hidden_size, hidden_size, bidirectional=True, batch_first=True)
        for name, param in self.gru.state_dict().items():
            if 'weight' in name: init.xavier_normal(param)
        self.dropout = nn.Dropout(0.1)

    def forward(self, contexts, word_embedding):
        '''
        contexts.size() -> (#batch, #sentence, #token)
        word_embedding() -> (#batch, #sentence x #token, #embedding)
        position_encoding() -> (#batch, #sentence, #embedding)
        facts.size() -> (#batch, #sentence, #hidden = #embedding)
        '''
        batch_num, sen_num, token_num = contexts.size()

        contexts = contexts.view(batch_num, -1)
        contexts = word_embedding(contexts)

        contexts = contexts.view(batch_num, sen_num, token_num, -1)
        contexts = position_encoding(contexts)
        contexts = self.dropout(contexts)

        h0 = Variable(torch.zeros(2, batch_num, self.hidden_size).cuda())
        facts, hdn = self.gru(contexts, h0)
        facts = facts[:, :, :hidden_size] + facts[:, :, hidden_size:]
        return facts

class AnswerModule(nn.Module):
    def __init__(self, vocab_size, hidden_size):
        super(AnswerModule, self).__init__()
        self.z = nn.Linear(2 * hidden_size, vocab_size)
        init.xavier_normal(self.z.state_dict()['weight'])
        self.dropout = nn.Dropout(0.1)

    def forward(self, M, questions):
        M = self.dropout(M)
        concat = torch.cat([M, questions], dim=2).squeeze(1)
        z = self.z(concat)
        return z

class DMNPlus(nn.Module):
    def __init__(self, hidden_size, vocab_size, num_hop=3, qa=None):
        super(DMNPlus, self).__init__()
        self.num_hop = num_hop
        self.qa = qa
        self.word_embedding = nn.Embedding(vocab_size, hidden_size, padding_idx=0, sparse=True).cuda()
        init.uniform(self.word_embedding.state_dict()['weight'], a=-(3**0.5), b=3**0.5)
        self.criterion = nn.CrossEntropyLoss(size_average=False)

        self.input_module = InputModule(vocab_size, hidden_size)
        self.question_module = QuestionModule(vocab_size, hidden_size)
        self.memory = EpisodicMemory(hidden_size)
        self.answer_module = AnswerModule(vocab_size, hidden_size)

    def forward(self, contexts, questions):
        '''
        contexts.size() -> (#batch, #sentence, #token) -> (#batch, #sentence, #hidden = #embedding)
        questions.size() -> (#batch, #token) -> (#batch, 1, #hidden)
        '''
        facts = self.input_module(contexts, self.word_embedding)
        questions = self.question_module(questions, self.word_embedding)
        M = questions
        for hop in range(self.num_hop):
            M = self.memory(facts, questions, M)
        preds = self.answer_module(M, questions)
        return preds

    def interpret_indexed_tensor(self, var):
        if len(var.size()) == 3:
            # var -> n x #sen x #token
            for n, sentences in enumerate(var):
                for i, sentence in enumerate(sentences):
                    s = ' '.join([self.qa.IVOCAB[elem.data[0]] for elem in sentence])
                    print('{}th of batch, {}th sentence, {}'.format(n, i, s))
        elif len(var.size()) == 2:
            # var -> n x #token
            for n, sentence in enumerate(var):
                s = ' '.join([self.qa.IVOCAB[elem.data[0]] for elem in sentence])
                print('{}th of batch, {}'.format(n, s))
        elif len(var.size()) == 1:
            # var -> n (one token per batch)
            for n, token in enumerate(var):
                s = self.qa.IVOCAB[token.data[0]]
                print('{}th of batch, {}'.format(n, s))

    def get_loss(self, contexts, questions, targets):
        output = self.forward(contexts, questions)
        loss = self.criterion(output, targets)
        reg_loss = 0
        for param in self.parameters():
            reg_loss += 0.001 * torch.sum(param * param)
        preds = F.softmax(output)
        _, pred_ids = torch.max(preds, dim=1)
        corrects = (pred_ids.data == answers.data)
        acc = torch.mean(corrects.float())
        return loss + reg_loss, acc

if __name__ == '__main__':
    train_idx = 0
    val_idx = 0
    test_idx = 0
    tf_summary_writer = tf.summary.FileWriter('save')
    for run in range(10):
        for task_id in range(1, 21):
            dset = Dataset(task_id)
            vocab_size = len(dset.QA.VOCAB)
            hidden_size = 80

            model = DMNPlus(hidden_size, vocab_size, num_hop=3, qa=dset.QA)
            model.cuda()
            early_stopping_cnt = 0
            early_stopping_flag = False
            best_acc = 0
            optim = torch.optim.Adam(model.parameters())


            for epoch in range(256):
                dset.set_mode('train')
                train_loader = DataLoader(
                    dset, batch_size=100, shuffle=True, collate_fn=pad_collate
                )

                model.train()
                if not early_stopping_flag:
                    total_acc = 0
                    cnt = 0
                    for batch_idx, data in enumerate(train_loader):
                        optim.zero_grad()
                        contexts, questions, answers = data
                        batch_size = contexts.size()[0]
                        contexts = Variable(contexts.long().cuda())
                        questions = Variable(questions.long().cuda())
                        answers = Variable(answers.cuda())

                        loss, acc = model.get_loss(contexts, questions, answers)
                        loss.backward()
                        total_acc += acc * batch_size
                        cnt += batch_size
                        train_idx = train_idx + 1
                        if batch_idx % 20 == 0:
                            print('Task {}, Epoch {} Training loss: {}, acc:{}, bidx:{}'.format(task_id,epoch,loss.data[0],total_acc / cnt,batch_idx))
                            add_summary_value(tf_summary_writer, 'Training loss', loss.data[0], train_idx)
                            add_summary_value(tf_summary_writer, 'Training acc', total_acc / cnt, train_idx)
                        optim.step()

                    dset.set_mode('valid')
                    valid_loader = DataLoader(
                        dset, batch_size=100, shuffle=False, collate_fn=pad_collate
                    )

                    model.eval()
                    total_acc = 0
                    cnt = 0
                    for batch_idx, data in enumerate(valid_loader):
                        contexts, questions, answers = data
                        batch_size = contexts.size()[0]
                        contexts = Variable(contexts.long().cuda())
                        questions = Variable(questions.long().cuda())
                        answers = Variable(answers.cuda())

                        loss, acc = model.get_loss(contexts, questions, answers)
                        total_acc += acc * batch_size
                        cnt += batch_size

                        val_idx = val_idx + 1
                        add_summary_value(tf_summary_writer, 'Valid loss', loss.data[0], val_idx)
                        add_summary_value(tf_summary_writer, 'Valid acc', total_acc/ cnt, val_idx)

                    total_acc = total_acc / cnt
                    if total_acc > best_acc:
                        best_acc = total_acc
                        best_state = model.state_dict()
                        early_stopping_cnt = 0
                    else:
                        early_stopping_cnt += 1
                        if early_stopping_cnt > 20:
                            early_stopping_flag = True

                    print('Run {}, Task {}, Epoch {} Validate Accuracy:{}'.format(run, task_id, epoch, total_acc))
                    if total_acc == 1.0:
                        break
                else:
                    print('Run {}, Task {}, Early Stopping at Epoch {} Valid Accuracy:{}'.format(run, task_id, epoch, best_acc))
                    break

            dset.set_mode('test')
            test_loader = DataLoader(
                dset, batch_size=100, shuffle=False, collate_fn=pad_collate
            )
            test_acc = 0
            cnt = 0

            for batch_idx, data in enumerate(test_loader):
                contexts, questions, answers = data
                batch_size = contexts.size()[0]
                contexts = Variable(contexts.long().cuda())
                questions = Variable(questions.long().cuda())
                answers = Variable(answers.cuda())

                model.load_state_dict(best_state)
                loss, acc = model.get_loss(contexts, questions, answers)
                test_acc += acc * batch_size
                cnt += batch_size

                test_idx = test_idx + 1
                add_summary_value(tf_summary_writer, 'Valid loss', loss.data[0], test_idx)
                add_summary_value(tf_summary_writer, 'Valid acc', test_acc / cnt, test_idx)

            print('Run {}, Task {}, Epoch {} Test Accuracy:{}'.format(run, task_id, epoch, test_acc / cnt))
            if not os.path.exists('save'):
                os.makedirs('save')
            model_id = 'save/task{}_epoch{}_run{}_acc{}.pth'.format(task_id, epoch, run, test_acc/cnt)
            with open(model_id, 'wb') as fp:
                torch.save(model.state_dict(), fp)